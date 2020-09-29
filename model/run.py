import argparse
import json
import os
import sys
import time

import boto3

import sagemaker
from sagemaker.image_uris import retrieve
from sagemaker.processing import Processor, ProcessingInput, ProcessingOutput
from sagemaker.model_monitor.dataset_format import DatasetFormat

import stepfunctions
from stepfunctions import steps
from stepfunctions.inputs import ExecutionInput
from stepfunctions.workflow import Workflow


def create_experiment_step(create_experiment_function_name):
    create_experiment_step = steps.compute.LambdaStep(
        "Create Experiment",
        parameters={
            "FunctionName": create_experiment_function_name,
            "Payload": {"ExperimentName.$": "$.ExperimentName", "TrialName.$": "$.TrialName",},
        },
        result_path="$.CreateTrialResults",
    )
    return create_experiment_step


def create_baseline_step(input_data, execution_input, region, role):
    # Define the enviornment
    dataset_format = DatasetFormat.csv()
    env = {
        "dataset_format": json.dumps(dataset_format),
        "dataset_source": "/opt/ml/processing/input/baseline_dataset_input",
        "output_path": "/opt/ml/processing/output",
        "publish_cloudwatch_metrics": "Disabled",  # Have to be disabled from processing job?
    }

    # Define the inputs and outputs
    inputs = [
        ProcessingInput(
            source=input_data["BaselineUri"],
            destination="/opt/ml/processing/input/baseline_dataset_input",
            input_name="baseline_dataset_input",
        ),
    ]
    outputs = [
        ProcessingOutput(
            source="/opt/ml/processing/output",
            destination=execution_input["BaselineOutputUri"],
            output_name="monitoring_output",
        ),
    ]

    # Get the default model monitor container
    monor_monitor_container_uri = retrieve(
        region=region, framework="model-monitor", version="latest"
    )

    # Create the processor
    monitor_analyzer = Processor(
        image_uri=monor_monitor_container_uri,
        role=role,
        instance_count=1,
        instance_type="ml.m5.xlarge",
        max_runtime_in_seconds=1800,
        env=env,
    )

    # Create the processing step
    baseline_step = steps.sagemaker.ProcessingStep(
        "Baseline Job",
        processor=monitor_analyzer,
        job_name=execution_input["BaselineJobName"],
        inputs=inputs,
        outputs=outputs,
        experiment_config={
            "ExperimentName": execution_input["ExperimentName"],  # '$.ExperimentName',
            "TrialName": execution_input["TrialName"],
            "TrialComponentDisplayName": "Baseline",
        },
        tags={
            "GitBranch": execution_input["GitBranch"],
            "GitCommitHash": execution_input["GitCommitHash"],
            "DataVersionId": execution_input["DataVersionId"],
        },
    )

    # Add the catch
    baseline_step.add_catch(
        steps.states.Catch(
            error_equals=["States.TaskFailed"],
            next_step=stepfunctions.steps.states.Fail(
                "Baseline failed", cause="SageMakerBaselineJobFailed"
            ),
        )
    )
    return baseline_step


def get_training_image(region):
    return sagemaker.image_uris.retrieve(region=region, framework="xgboost", version="latest")


def create_training_step(
    image_uri,
    hyperparameters,
    input_data,
    output_data,
    execution_input,
    query_training_function_name,
    region,
    role,
):
    # Create the estimator
    xgb = sagemaker.estimator.Estimator(
        image_uri,
        role,
        instance_count=1,
        instance_type="ml.m4.xlarge",
        output_path=output_data["ModelOutputUri"],  # NOTE: Can't use execution_input here
    )

    # Set the hyperparameters overriding with any defaults
    hp = {
        "max_depth": "9",
        "eta": "0.2",
        "gamma": "4",
        "min_child_weight": "300",
        "subsample": "0.8",
        "objective": "reg:linear",
        "early_stopping_rounds": "10",
        "num_round": "100",
    }
    xgb.set_hyperparameters(**{**hp, **hyperparameters})

    # Specify the data source
    s3_input_train = sagemaker.inputs.TrainingInput(
        s3_data=input_data["TrainingUri"], content_type="csv"
    )
    s3_input_val = sagemaker.inputs.TrainingInput(
        s3_data=input_data["ValidationUri"], content_type="csv"
    )
    data = {"train": s3_input_train, "validation": s3_input_val}

    # Create the training step
    training_step = steps.TrainingStep(
        "Training Job",
        estimator=xgb,
        data=data,
        job_name=execution_input["TrainingJobName"],
        experiment_config={
            "ExperimentName": execution_input["ExperimentName"],
            "TrialName": execution_input["TrialName"],
            "TrialComponentDisplayName": "Training",
        },
        tags={
            "GitBranch": execution_input["GitBranch"],
            "GitCommitHash": execution_input["GitCommitHash"],
            "DataVersionId": execution_input["DataVersionId"],
        },
        result_path="$.TrainingResults",
    )

    # Add the catch
    training_step.add_catch(
        stepfunctions.steps.states.Catch(
            error_equals=["States.TaskFailed"],
            next_step=stepfunctions.steps.states.Fail(
                "Training failed", cause="SageMakerTrainingJobFailed"
            ),
        )
    )

    # Must follow the training test
    model_step = steps.sagemaker.ModelStep(
        "Save Model",
        input_path="$.TrainingResults",
        model=training_step.get_expected_model(),
        model_name=execution_input["TrainingJobName"],
        result_path="$.ModelStepResults",
    )

    # Query the training step
    training_query_step = steps.compute.LambdaStep(
        "Query Training Results",
        parameters={
            "FunctionName": query_training_function_name,
            "Payload": {"TrainingJobName.$": "$.TrainingJobName"},
        },
        result_path="$.QueryTrainingResults",
    )

    check_accuracy_fail_step = steps.states.Fail(
        "Model Error Too Low", comment="RMSE accuracy higher than threshold"
    )

    check_accuracy_succeed_step = steps.states.Succeed("Model Error Acceptable")

    # TODO: Update query method to query validation error using better result path
    threshold_rule = steps.choice_rule.ChoiceRule.NumericLessThan(
        variable=training_query_step.output()["QueryTrainingResults"]["Payload"]["results"][
            "TrainingMetrics"
        ][0]["Value"],
        value=10,
    )

    check_accuracy_step = steps.states.Choice("RMSE < 10")

    check_accuracy_step.add_choice(rule=threshold_rule, next_step=check_accuracy_succeed_step)
    check_accuracy_step.default_choice(next_step=check_accuracy_fail_step)

    # Return the chain of these steps
    return steps.states.Chain([training_step, model_step, training_query_step, check_accuracy_step])


def create_graph(create_experiment_step, baseline_step, training_step):
    sagemaker_jobs = steps.states.Parallel("SageMaker Jobs")
    sagemaker_jobs.add_branch(baseline_step)
    sagemaker_jobs.add_branch(training_step)

    # Do we need specific failure for the jobs for group?
    sagemaker_jobs.add_catch(
        stepfunctions.steps.states.Catch(
            error_equals=["States.TaskFailed"],
            next_step=stepfunctions.steps.states.Fail(
                "SageMaker Jobs failed", cause="SageMakerJobsFailed"
            ),
        )
    )

    # Return the workflow graph
    return steps.states.Chain([create_experiment_step, sagemaker_jobs])


def get_dev_params(model_name, job_id, role, image_uri, kms_key_id):
    return {
        "Parameters": {
            "ImageRepoUri": image_uri,
            "ModelName": model_name,
            "TrainJobId": job_id,
            "MLOpsRoleArn": role,
            "VariantName": "dev-{}".format(model_name),
            "KmsKeyId": kms_key_id,
        }
    }


def get_prd_params(model_name, job_id, role, image_uri, kms_key_id):
    dev_params = get_dev_params(model_name, job_id, role, image_uri, kms_key_id)["Parameters"]
    prod_params = {
        "VariantName": "prd-{}".format(model_name),
        "ScheduleMetricName": "feature_baseline_drift_total_amount",
        "ScheduleMetricThreshold": str("0.20"),
    }
    return {"Parameters": dict(dev_params, **prod_params)}


def get_pipeline_revision(pipeline_name):
    # Get pipeline execution id
    codepipeline = boto3.client("codepipeline")
    response = codepipeline.get_pipeline_state(name=pipeline_name)
    # Get the GitSource and DataSource and JobId
    rev = dict(
        [
            (r["actionName"], r["currentRevision"]["revisionId"])
            for r in response["stageStates"][0]["actionStates"]
        ]
    )
    rev["JobId"] = response["stageStates"][0]["latestExecution"]["pipelineExecutionId"]
    return rev


def main(
    git_branch,
    pipeline_name,
    model_name,
    deploy_role,
    sagemaker_role,
    sagemaker_bucket,
    data_dir,
    output_dir,
    ecr_dir,
    kms_key_id,
    workflow_pipeline_arn,
    create_experiment_function_name,
    query_training_function_name,
):
    # Get the region
    region = boto3.Session().region_name
    print("region: {}".format(region))

    if ecr_dir:
        # Load the image uri and input data config
        with open(os.path.join(ecr_dir, "imageDetail.json"), "r") as f:
            image_uri = json.load(f)["ImageURI"]
    else:
        # Get the the managed image uri for current region
        image_uri = get_training_image(region)
    print("image uri: {}".format(image_uri))

    with open(os.path.join(data_dir, "inputData.json"), "r") as f:
        input_data = json.load(f)
        print("training uri: {}".format(input_data["TrainingUri"]))
        print("validation uri: {}".format(input_data["ValidationUri"]))
        print("baseline uri: {}".format(input_data["BaselineUri"]))

    # Get the job id and source revisions
    revision = get_pipeline_revision(pipeline_name)
    job_id = revision["JobId"]
    git_commit_id = revision["GitSource"]
    data_verison_id = revision["DataSource"]
    print("job id: {}".format(job_id))
    print("git commit: {}".format(git_commit_id))
    print("data version: {}".format(data_verison_id))

    # Set the output Data
    output_data = {
        "ModelOutputUri": "s3://{}/{}/model".format(sagemaker_bucket, model_name),
        "BaselineOutputUri": f"s3://{sagemaker_bucket}/{model_name}/monitoring/baseline/mlops-{model_name}-pbl-{job_id}",
    }
    print("model output uri: {}".format(output_data["ModelOutputUri"]))

    # Pass these into the training method
    hyperparameters = {}
    if os.path.exists(os.path.join(data_dir, "hyperparameters.json")):
        with open(os.path.join(data_dir, "hyperparameters.json"), "r") as f:
            hyperparameters = json.load(f)
            for i in hyperparameters:
                hyperparameters[i] = str(hyperparameters[i])

    # Define the step functions execution input schema
    execution_input = ExecutionInput(
        schema={
            "GitBranch": str,
            "GitCommitHash": str,
            "DataVersionId": str,
            "ExperimentName": str,
            "TrialName": str,
            "BaselineJobName": str,
            "BaselineOutputUri": str,
            "TrainingJobName": str,
        }
    )

    # Create experiment step
    experiment_step = create_experiment_step(create_experiment_function_name)
    baseline_step = create_baseline_step(input_data, execution_input, region, sagemaker_role)
    training_step = create_training_step(
        image_uri,
        hyperparameters,
        input_data,
        output_data,
        execution_input,
        query_training_function_name,
        region,
        sagemaker_role,
    )
    workflow_graph = create_graph(experiment_step, baseline_step, training_step)

    # Update the workflow
    workflow = Workflow.attach(workflow_pipeline_arn)
    workflow.update(workflow_graph)
    print("Updating workflow: {}".format(workflow.state_machine_arn))

    # Create output directory
    if not os.path.exists(output_dir):
        os.mkdir(output_dir)

    # Write the workflow graph to json
    with open(os.path.join(output_dir, "workflow-graph.json"), "w") as f:
        f.write(workflow.definition.to_json(pretty=True))

    # Define the experiment and trial name based on model name and job id
    experiment_name = "mlops-{}".format(model_name)
    trial_name = "mlops-{}-{}".format(model_name, job_id)

    # Write the workflow inputs to file
    with open(os.path.join(output_dir, "workflow-input.json"), "w") as f:
        workflow_inputs = {
            "ExperimentName": experiment_name,
            "TrialName": trial_name,
            "GitBranch": git_branch,
            "GitCommitHash": git_commit_id,
            "DataVersionId": data_verison_id,
            "BaselineJobName": trial_name,
            "BaselineOutputUri": output_data["BaselineOutputUri"],
            "TrainingJobName": trial_name,
        }
        json.dump(workflow_inputs, f)

    # Write the dev & prod params for CFN
    with open(os.path.join(output_dir, "deploy-model-dev.json"), "w") as f:
        params = get_dev_params(model_name, job_id, deploy_role, image_uri, kms_key_id)
        json.dump(params, f)
    with open(os.path.join(output_dir, "template-model-prd.json"), "w") as f:
        params = get_prd_params(model_name, job_id, deploy_role, image_uri, kms_key_id)
        json.dump(params, f)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Load parameters")
    parser.add_argument("--data-dir", required=True)
    parser.add_argument("--output-dir", required=True)
    parser.add_argument("--ecr-dir", required=False)
    parser.add_argument("--pipeline-name", required=True)
    parser.add_argument("--model-name", required=True)
    parser.add_argument("--deploy-role", required=True)
    parser.add_argument("--sagemaker-role", required=True)
    parser.add_argument("--sagemaker-bucket", required=True)
    parser.add_argument("--kms-key-id", required=True)
    parser.add_argument("--git-branch", required=True)
    parser.add_argument("--workflow-pipeline-arn", required=True)
    parser.add_argument("--create-experiment-function-name", required=True)
    parser.add_argument("--query-training-function-name", required=True)
    args = vars(parser.parse_args())
    print("args: {}".format(args))
    main(**args)
