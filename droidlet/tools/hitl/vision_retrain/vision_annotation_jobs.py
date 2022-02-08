"""
Copyright (c) Facebook, Inc. and its affiliates.
"""
import logging
import os
import signal
import subprocess
import time
import csv
import yaml
import json
import copy

import boto3

from droidlet.tools.hitl.data_generator import DataGenerator
from mephisto.abstractions.databases.local_database import LocalMephistoDB
from mephisto.tools.data_browser import DataBrowser as MephistoDataBrowser

db = LocalMephistoDB()
mephisto_data_browser = MephistoDataBrowser(db=db)

HITL_TMP_DIR = (
    os.environ["HITL_TMP_DIR"] if os.getenv("HITL_TMP_DIR") else f"{os.path.expanduser('~')}/.hitl"
)
ANNOTATION_JOB_POLL_TIME = 30
ANNOTATION_PROCESS_TIMEOUT_DEFAULT = 300
S3_BUCKET_NAME = "droidlet-hitl"
S3_ROOT = "s3://droidlet-hitl"

AWS_ACCESS_KEY_ID = os.environ["AWS_ACCESS_KEY_ID"]
AWS_SECRET_ACCESS_KEY = os.environ["AWS_SECRET_ACCESS_KEY"]
AWS_DEFAULT_REGION = os.environ["AWS_DEFAULT_REGION"]
MEPHISTO_REQUESTER = os.environ["MEPHISTO_REQUESTER"]

s3 = boto3.client(
    "s3",
    aws_access_key_id=AWS_ACCESS_KEY_ID,
    aws_secret_access_key=AWS_SECRET_ACCESS_KEY,
    region_name=AWS_DEFAULT_REGION,
)

logging.basicConfig(level="INFO")


class VisionAnnotationJob(DataGenerator):
    """
    This Data Generator is responsible for spinning up Vision Annotation Jobs.

    Each Vision Annotation Job is a single HIT where turkers are asked to annotate the visual scene
    captured at the time a command was issued and labeled as containing a perception error.

    On a high level:
    - The inputs of this data generator are a list of scenes and corresponding timestamps at which they were captured 
      (used as the unique scene identifier)
    - The output of this data generator is the same visual scene and label, with a new field representing
      the instance segmentation mask.

    """

    def __init__(self, batch_id: int, timestamp: str, scenes: list, timeout: float = ANNOTATION_PROCESS_TIMEOUT_DEFAULT) -> None:
        super(VisionAnnotationJob, self).__init__(timeout)
        self._batch_id = batch_id
        self._timestamp = timestamp
        self._scenes = scenes

    def run(self) -> None:
        try:
            # Put the scene in extra_refs
            logging.info("Saving scene list to extra_refs")
            scene_ref_filepath = os.path.join(os.getcwd(), "../../crowdsourcing/vision_annotation_task/server_files/extra_refs/scene_list.json")
            with open(scene_ref_filepath, "w") as f:
                json.dump(self._scenes, f)

            # Write scene indeces and labels to a new data.csv for Mephisto to read
            logging.info(f"Writing HIT data to {self._timestamp}data.csv")
            data_csv_path = os.path.join(os.getcwd(), f"../../crowdsourcing/vision_annotation_task/{self._timestamp}data.csv")
            with open(data_csv_path, "w") as f:
                csv_writer = csv.writer(f, delimiter=",")
                csv_writer.writerow(["batch_id", "scene_idx", "label"])
                for i in range(len(self._scenes)):
                    csv_writer.writerow([str(self._batch_id), str(i), self._scenes[i]["obj_ref"]])

            # Edit Mephisto config file to have the right task name and data csv file
            with open("../../crowdsourcing/vision_annotation_task/conf/annotation.yaml", "r") as stream:
                config = yaml.safe_load(stream)
                task_name = "ca-vis-anno" + str(self._batch_id)
                config["mephisto"]["blueprint"]["data_csv"] = f"${{task_dir}}/{self._timestamp}data.csv"
                config["mephisto"]["task"]["task_name"] = task_name
            logging.info(f"Updating Mephisto config file to have task_name: {task_name}")
            with open("../../crowdsourcing/vision_annotation_task/conf/annotation.yaml", "w") as stream:
                stream.write("#@package _global_\n")
                yaml.dump(config, stream)

            # Launch the batch of HITs
            anno_job_path = os.path.join(os.getcwd(), "../../crowdsourcing/vision_annotation_task/run_annotation_with_qual.py")
            anno_cmd = "echo -ne '\n' | python3 " + anno_job_path + \
                " mephisto.provider.requester_name=" + MEPHISTO_REQUESTER + \
                " mephisto.architect.profile_name=mephisto-router-iam"
            p = subprocess.Popen(anno_cmd, shell=True, preexec_fn=os.setsid)

            # Keep running Mephisto until timeout or job finished
            while not self.check_is_timeout() and p.poll() is None:
                logging.info(f"Vision Annotation Job [{self._batch_id}] still running...Remaining time: {self.get_remaining_time()}")
                time.sleep(ANNOTATION_JOB_POLL_TIME)

            if p.poll() is None:
                # If mturk job is still running after timeout, terminate it
                logging.info(f"Manually terminate turk job after timeout...")
                os.killpg(os.getpgid(p.pid), signal.SIGINT)
                time.sleep(300)
                os.killpg(os.getpgid(p.pid), signal.SIGINT)
                time.sleep(300)
                os.killpg(os.getpgid(p.pid), signal.SIGKILL)

            # Load annotated scene data into output format
            logging.info(f"Retrieving data from Mephisto")
            units = mephisto_data_browser.get_units_for_task_name(task_name)
            output_scene = copy.deepcopy(self._scenes)
            for i in range(len(units)):
                data = mephisto_data_browser.get_data_from_unit(units[i])
                output_scene[i]["inst_seg_tags"] = json.loads(data["data"]["outputs"]["inst_seg_tags"])

            # Upload vision annotation results to S3 and save locally
            logging.info(f"Uploading scene to S3 {self._batch_id}/annotated_scenes/{self._timestamp}.json")
            os.makedirs(os.path.join(HITL_TMP_DIR, f"{self._batch_id}/annotated_scenes"), exist_ok=True)
            results_json = os.path.join(HITL_TMP_DIR, f"{self._batch_id}/annotated_scenes/{self._timestamp}.json")
            with open(results_json, "w") as f:
                json.dump(output_scene, f)
            with open(results_json, "rb") as f:
                s3.upload_fileobj(f, f"{S3_BUCKET_NAME}", f"{self._batch_id}/annotated_scenes/{self._timestamp}.json")
            logging.info(f"Uploading completed")

            # Delete the scene file from extra_refs and the bespoke data csv
            os.remove(scene_ref_filepath)
            os.remove(data_csv_path)

        except:
            logging.info(f"Annotation Job [{self._batch_id}] terminated unexpectedly...")

        self.set_finished()


if __name__ == "__main__":
    example_scene = [{"obj_ref": "floating blue hollow sqaure", "avatarInfo": {"pos": [1, 3, 1], "look": [0.0, 0.0]}, "agentInfo": {"pos": [3, 3, 0], "look": [0.0, 0.0]}, "inst_seg_tags": [{"tags": ["HOLLOW_RECTANGLE"], "locs": [[-6, 7, -2], [-6, 7, -1], [-6, 7, 0], [-6, 7, 1], [-5, 7, -2], [-5, 7, 1], [-4, 7, -2], [-4, 7, 1], [-3, 7, -2], [-3, 7, -1], [-3, 7, 0], [-3, 7, 1]]}], "blocks": [[-9, 0, -9, 46], [-9, 0, -8, 46], [-9, 0, -7, 46], [-9, 0, -6, 46], [-9, 0, -5, 46], [-9, 0, -4, 46], [-9, 0, -3, 46], [-9, 0, -2, 46], [-9, 0, -1, 46], [-9, 0, 0, 46], [-9, 0, 1, 46], [-9, 0, 2, 46], [-9, 0, 3, 46], [-9, 0, 4, 46], [-9, 0, 5, 46], [-9, 0, 6, 46], [-9, 0, 7, 46], [-9, 0, 8, 46], [-9, 0, 9, 46], [-9, 0, 10, 46], [-9, 1, -9, 46], [-9, 1, -8, 46], [-9, 1, -7, 46], [-9, 1, -6, 46], [-9, 1, -5, 46], [-9, 1, -4, 46], [-9, 1, -3, 46], [-9, 1, -2, 46], [-9, 1, -1, 46], [-9, 1, 0, 46], [-9, 1, 1, 46], [-9, 1, 2, 46], [-9, 1, 3, 46], [-9, 1, 4, 46], [-9, 1, 5, 46], [-9, 1, 6, 46], [-9, 1, 7, 46], [-9, 1, 8, 46], [-9, 1, 9, 46], [-9, 1, 10, 46], [-9, 2, -9, 46], [-9, 2, -8, 46], [-9, 2, -7, 46], [-9, 2, -6, 46], [-9, 2, -5, 46], [-9, 2, -4, 46], [-9, 2, -3, 46], [-9, 2, -2, 46], [-9, 2, -1, 46], [-9, 2, 0, 46], [-9, 2, 1, 46], [-9, 2, 2, 46], [-9, 2, 3, 46], [-9, 2, 4, 46], [-9, 2, 5, 46], [-9, 2, 6, 46], [-9, 2, 7, 46], [-9, 2, 8, 46], [-9, 2, 9, 46], [-9, 2, 10, 46], [-8, 0, -9, 46], [-8, 0, -8, 46], [-8, 0, -7, 46], [-8, 0, -6, 46], [-8, 0, -5, 46], [-8, 0, -4, 46], [-8, 0, -3, 46], [-8, 0, -2, 46], [-8, 0, -1, 46], [-8, 0, 0, 46], [-8, 0, 1, 46], [-8, 0, 2, 46], [-8, 0, 3, 46], [-8, 0, 4, 46], [-8, 0, 5, 46], [-8, 0, 6, 46], [-8, 0, 7, 46], [-8, 0, 8, 46], [-8, 0, 9, 46], [-8, 0, 10, 46], [-8, 1, -9, 46], [-8, 1, -8, 46], [-8, 1, -7, 46], [-8, 1, -6, 46], [-8, 1, -5, 46], [-8, 1, -4, 46], [-8, 1, -3, 46], [-8, 1, -2, 46], [-8, 1, -1, 46], [-8, 1, 0, 46], [-8, 1, 1, 46], [-8, 1, 2, 46], [-8, 1, 3, 46], [-8, 1, 4, 46], [-8, 1, 5, 46], [-8, 1, 6, 46], [-8, 1, 7, 46], [-8, 1, 8, 46], [-8, 1, 9, 46], [-8, 1, 10, 46], [-8, 2, -9, 46], [-8, 2, -8, 46], [-8, 2, -7, 46], [-8, 2, -6, 46], [-8, 2, -5, 46], [-8, 2, -4, 46], [-8, 2, -3, 46], [-8, 2, -2, 46], [-8, 2, -1, 46], [-8, 2, 0, 46], [-8, 2, 1, 46], [-8, 2, 2, 46], [-8, 2, 3, 46], [-8, 2, 4, 46], [-8, 2, 5, 46], [-8, 2, 6, 46], [-8, 2, 7, 46], [-8, 2, 8, 46], [-8, 2, 9, 46], [-8, 2, 10, 46], [-7, 0, -9, 46], [-7, 0, -8, 46], [-7, 0, -7, 46], [-7, 0, -6, 46], [-7, 0, -5, 46], [-7, 0, -4, 46], [-7, 0, -3, 46], [-7, 0, -2, 46], [-7, 0, -1, 46], [-7, 0, 0, 46], [-7, 0, 1, 46], [-7, 0, 2, 46], [-7, 0, 3, 46], [-7, 0, 4, 46], [-7, 0, 5, 46], [-7, 0, 6, 46], [-7, 0, 7, 46], [-7, 0, 8, 46], [-7, 0, 9, 46], [-7, 0, 10, 46], [-7, 1, -9, 46], [-7, 1, -8, 46], [-7, 1, -7, 46], [-7, 1, -6, 46], [-7, 1, -5, 46], [-7, 1, -4, 46], [-7, 1, -3, 46], [-7, 1, -2, 46], [-7, 1, -1, 46], [-7, 1, 0, 46], [-7, 1, 1, 46], [-7, 1, 2, 46], [-7, 1, 3, 46], [-7, 1, 4, 46], [-7, 1, 5, 46], [-7, 1, 6, 46], [-7, 1, 7, 46], [-7, 1, 8, 46], [-7, 1, 9, 46], [-7, 1, 10, 46], [-7, 2, -9, 46], [-7, 2, -8, 46], [-7, 2, -7, 46], [-7, 2, -6, 46], [-7, 2, -5, 46], [-7, 2, -4, 46], [-7, 2, -3, 46], [-7, 2, -2, 46], [-7, 2, -1, 46], [-7, 2, 0, 46], [-7, 2, 1, 46], [-7, 2, 2, 46], [-7, 2, 3, 46], [-7, 2, 4, 46], [-7, 2, 5, 46], [-7, 2, 6, 46], [-7, 2, 7, 46], [-7, 2, 8, 46], [-7, 2, 9, 46], [-7, 2, 10, 46], [-6, 0, -9, 46], [-6, 0, -8, 46], [-6, 0, -7, 46], [-6, 0, -6, 46], [-6, 0, -5, 46], [-6, 0, -4, 46], [-6, 0, -3, 46], [-6, 0, -2, 46], [-6, 0, -1, 46], [-6, 0, 0, 46], [-6, 0, 1, 46], [-6, 0, 2, 46], [-6, 0, 3, 46], [-6, 0, 4, 46], [-6, 0, 5, 46], [-6, 0, 6, 46], [-6, 0, 7, 46], [-6, 0, 8, 46], [-6, 0, 9, 46], [-6, 0, 10, 46], [-6, 1, -9, 46], [-6, 1, -8, 46], [-6, 1, -7, 46], [-6, 1, -6, 46], [-6, 1, -5, 46], [-6, 1, -4, 46], [-6, 1, -3, 46], [-6, 1, -2, 46], [-6, 1, -1, 46], [-6, 1, 0, 46], [-6, 1, 1, 46], [-6, 1, 2, 46], [-6, 1, 3, 46], [-6, 1, 4, 46], [-6, 1, 5, 46], [-6, 1, 6, 46], [-6, 1, 7, 46], [-6, 1, 8, 46], [-6, 1, 9, 46], [-6, 1, 10, 46], [-6, 2, -9, 46], [-6, 2, -8, 46], [-6, 2, -7, 46], [-6, 2, -6, 46], [-6, 2, -5, 46], [-6, 2, -4, 46], [-6, 2, -3, 46], [-6, 2, -2, 46], [-6, 2, -1, 46], [-6, 2, 0, 46], [-6, 2, 1, 46], [-6, 2, 2, 46], [-6, 2, 3, 46], [-6, 2, 4, 46], [-6, 2, 5, 46], [-6, 2, 6, 46], [-6, 2, 7, 46], [-6, 2, 8, 46], [-6, 2, 9, 46], [-6, 2, 10, 46], [-5, 0, -9, 46], [-5, 0, -8, 46], [-5, 0, -7, 46], [-5, 0, -6, 46], [-5, 0, -5, 46], [-5, 0, -4, 46], [-5, 0, -3, 46], [-5, 0, -2, 46], [-5, 0, -1, 46], [-5, 0, 0, 46], [-5, 0, 1, 46], [-5, 0, 2, 46], [-5, 0, 3, 46], [-5, 0, 4, 46], [-5, 0, 5, 46], [-5, 0, 6, 46], [-5, 0, 7, 46], [-5, 0, 8, 46], [-5, 0, 9, 46], [-5, 0, 10, 46], [-5, 1, -9, 46], [-5, 1, -8, 46], [-5, 1, -7, 46], [-5, 1, -6, 46], [-5, 1, -5, 46], [-5, 1, -4, 46], [-5, 1, -3, 46], [-5, 1, -2, 46], [-5, 1, -1, 46], [-5, 1, 0, 46], [-5, 1, 1, 46], [-5, 1, 2, 46], [-5, 1, 3, 46], [-5, 1, 4, 46], [-5, 1, 5, 46], [-5, 1, 6, 46], [-5, 1, 7, 46], [-5, 1, 8, 46], [-5, 1, 9, 46], [-5, 1, 10, 46], [-5, 2, -9, 46], [-5, 2, -8, 46], [-5, 2, -7, 46], [-5, 2, -6, 46], [-5, 2, -5, 46], [-5, 2, -4, 46], [-5, 2, -3, 46], [-5, 2, -2, 46], [-5, 2, -1, 46], [-5, 2, 0, 46], [-5, 2, 1, 46], [-5, 2, 2, 46], [-5, 2, 3, 46], [-5, 2, 4, 46], [-5, 2, 5, 46], [-5, 2, 6, 46], [-5, 2, 7, 46], [-5, 2, 8, 46], [-5, 2, 9, 46], [-5, 2, 10, 46], [-4, 0, -9, 46], [-4, 0, -8, 46], [-4, 0, -7, 46], [-4, 0, -6, 46], [-4, 0, -5, 46], [-4, 0, -4, 46], [-4, 0, -3, 46], [-4, 0, -2, 46], [-4, 0, -1, 46], [-4, 0, 0, 46], [-4, 0, 1, 46], [-4, 0, 2, 46], [-4, 0, 3, 46], [-4, 0, 4, 46], [-4, 0, 5, 46], [-4, 0, 6, 46], [-4, 0, 7, 46], [-4, 0, 8, 46], [-4, 0, 9, 46], [-4, 0, 10, 46], [-4, 1, -9, 46], [-4, 1, -8, 46], [-4, 1, -7, 46], [-4, 1, -6, 46], [-4, 1, -5, 46], [-4, 1, -4, 46], [-4, 1, -3, 46], [-4, 1, -2, 46], [-4, 1, -1, 46], [-4, 1, 0, 46], [-4, 1, 1, 46], [-4, 1, 2, 46], [-4, 1, 3, 46], [-4, 1, 4, 46], [-4, 1, 5, 46], [-4, 1, 6, 46], [-4, 1, 7, 46], [-4, 1, 8, 46], [-4, 1, 9, 46], [-4, 1, 10, 46], [-4, 2, -9, 46], [-4, 2, -8, 46], [-4, 2, -7, 46], [-4, 2, -6, 46], [-4, 2, -5, 46], [-4, 2, -4, 46], [-4, 2, -3, 46], [-4, 2, -2, 46], [-4, 2, -1, 46], [-4, 2, 0, 46], [-4, 2, 1, 46], [-4, 2, 2, 46], [-4, 2, 3, 46], [-4, 2, 4, 46], [-4, 2, 5, 46], [-4, 2, 6, 46], [-4, 2, 7, 46], [-4, 2, 8, 46], [-4, 2, 9, 46], [-4, 2, 10, 46], [-3, 0, -9, 46], [-3, 0, -8, 46], [-3, 0, -7, 46], [-3, 0, -6, 46], [-3, 0, -5, 46], [-3, 0, -4, 46], [-3, 0, -3, 46], [-3, 0, -2, 46], [-3, 0, -1, 46], [-3, 0, 0, 46], [-3, 0, 1, 46], [-3, 0, 2, 46], [-3, 0, 3, 46], [-3, 0, 4, 46], [-3, 0, 5, 46], [-3, 0, 6, 46], [-3, 0, 7, 46], [-3, 0, 8, 46], [-3, 0, 9, 46], [-3, 0, 10, 46], [-3, 1, -9, 46], [-3, 1, -8, 46], [-3, 1, -7, 46], [-3, 1, -6, 46], [-3, 1, -5, 46], [-3, 1, -4, 46], [-3, 1, -3, 46], [-3, 1, -2, 46], [-3, 1, -1, 46], [-3, 1, 0, 46], [-3, 1, 1, 46], [-3, 1, 2, 46], [-3, 1, 3, 46], [-3, 1, 4, 46], [-3, 1, 5, 46], [-3, 1, 6, 46], [-3, 1, 7, 46], [-3, 1, 8, 46], [-3, 1, 9, 46], [-3, 1, 10, 46], [-3, 2, -9, 46], [-3, 2, -8, 46], [-3, 2, -7, 46], [-3, 2, -6, 46], [-3, 2, -5, 46], [-3, 2, -4, 46], [-3, 2, -3, 46], [-3, 2, -2, 46], [-3, 2, -1, 46], [-3, 2, 0, 46], [-3, 2, 1, 46], [-3, 2, 2, 46], [-3, 2, 3, 46], [-3, 2, 4, 46], [-3, 2, 5, 46], [-3, 2, 6, 46], [-3, 2, 7, 46], [-3, 2, 8, 46], [-3, 2, 9, 46], [-3, 2, 10, 46], [-2, 0, -9, 46], [-2, 0, -8, 46], [-2, 0, -7, 46], [-2, 0, -6, 46], [-2, 0, -5, 46], [-2, 0, -4, 46], [-2, 0, -3, 46], [-2, 0, -2, 46], [-2, 0, -1, 46], [-2, 0, 0, 46], [-2, 0, 1, 46], [-2, 0, 2, 46], [-2, 0, 3, 46], [-2, 0, 4, 46], [-2, 0, 5, 46], [-2, 0, 6, 46], [-2, 0, 7, 46], [-2, 0, 8, 46], [-2, 0, 9, 46], [-2, 0, 10, 46], [-2, 1, -9, 46], [-2, 1, -8, 46], [-2, 1, -7, 46], [-2, 1, -6, 46], [-2, 1, -5, 46], [-2, 1, -4, 46], [-2, 1, -3, 46], [-2, 1, -2, 46], [-2, 1, -1, 46], [-2, 1, 0, 46], [-2, 1, 1, 46], [-2, 1, 2, 46], [-2, 1, 3, 46], [-2, 1, 4, 46], [-2, 1, 5, 46], [-2, 1, 6, 46], [-2, 1, 7, 46], [-2, 1, 8, 46], [-2, 1, 9, 46], [-2, 1, 10, 46], [-2, 2, -9, 46], [-2, 2, -8, 46], [-2, 2, -7, 46], [-2, 2, -6, 46], [-2, 2, -5, 46], [-2, 2, -4, 46], [-2, 2, -3, 46], [-2, 2, -2, 46], [-2, 2, -1, 46], [-2, 2, 0, 46], [-2, 2, 1, 46], [-2, 2, 2, 46], [-2, 2, 3, 46], [-2, 2, 4, 46], [-2, 2, 5, 46], [-2, 2, 6, 46], [-2, 2, 7, 46], [-2, 2, 8, 46], [-2, 2, 9, 46], [-2, 2, 10, 46], [-1, 0, -9, 46], [-1, 0, -8, 46], [-1, 0, -7, 46], [-1, 0, -6, 46], [-1, 0, -5, 46], [-1, 0, -4, 46], [-1, 0, -3, 46], [-1, 0, -2, 46], [-1, 0, -1, 46], [-1, 0, 0, 46], [-1, 0, 1, 46], [-1, 0, 2, 46], [-1, 0, 3, 46], [-1, 0, 4, 46], [-1, 0, 5, 46], [-1, 0, 6, 46], [-1, 0, 7, 46], [-1, 0, 8, 46], [-1, 0, 9, 46], [-1, 0, 10, 46], [-1, 1, -9, 46], [-1, 1, -8, 46], [-1, 1, -7, 46], [-1, 1, -6, 46], [-1, 1, -5, 46], [-1, 1, -4, 46], [-1, 1, -3, 46], [-1, 1, -2, 46], [-1, 1, -1, 46], [-1, 1, 0, 46], [-1, 1, 1, 46], [-1, 1, 2, 46], [-1, 1, 3, 46], [-1, 1, 4, 46], [-1, 1, 5, 46], [-1, 1, 6, 46], [-1, 1, 7, 46], [-1, 1, 8, 46], [-1, 1, 9, 46], [-1, 1, 10, 46], [-1, 2, -9, 46], [-1, 2, -8, 46], [-1, 2, -7, 46], [-1, 2, -6, 46], [-1, 2, -5, 46], [-1, 2, -4, 46], [-1, 2, -3, 46], [-1, 2, -2, 46], [-1, 2, -1, 46], [-1, 2, 0, 46], [-1, 2, 1, 46], [-1, 2, 2, 46], [-1, 2, 3, 46], [-1, 2, 4, 46], [-1, 2, 5, 46], [-1, 2, 6, 46], [-1, 2, 7, 46], [-1, 2, 8, 46], [-1, 2, 9, 46], [-1, 2, 10, 46], [0, 0, -9, 46], [0, 0, -8, 46], [0, 0, -7, 46], [0, 0, -6, 46], [0, 0, -5, 46], [0, 0, -4, 46], [0, 0, -3, 46], [0, 0, -2, 46], [0, 0, -1, 46], [0, 0, 0, 46], [0, 0, 1, 46], [0, 0, 2, 46], [0, 0, 3, 46], [0, 0, 4, 46], [0, 0, 5, 46], [0, 0, 6, 46], [0, 0, 7, 46], [0, 0, 8, 46], [0, 0, 9, 46], [0, 0, 10, 46], [0, 1, -9, 46], [0, 1, -8, 46], [0, 1, -7, 46], [0, 1, -6, 46], [0, 1, -5, 46], [0, 1, -4, 46], [0, 1, -3, 46], [0, 1, -2, 46], [0, 1, -1, 46], [0, 1, 0, 46], [0, 1, 1, 46], [0, 1, 2, 46], [0, 1, 3, 46], [0, 1, 4, 46], [0, 1, 5, 46], [0, 1, 6, 46], [0, 1, 7, 46], [0, 1, 8, 46], [0, 1, 9, 46], [0, 1, 10, 46], [0, 2, -9, 46], [0, 2, -8, 46], [0, 2, -7, 46], [0, 2, -6, 46], [0, 2, -5, 46], [0, 2, -4, 46], [0, 2, -3, 46], [0, 2, -2, 46], [0, 2, -1, 46], [0, 2, 0, 46], [0, 2, 1, 46], [0, 2, 2, 46], [0, 2, 3, 46], [0, 2, 4, 46], [0, 2, 5, 46], [0, 2, 6, 46], [0, 2, 7, 46], [0, 2, 8, 46], [0, 2, 9, 46], [0, 2, 10, 46], [1, 0, -9, 46], [1, 0, -8, 46], [1, 0, -7, 46], [1, 0, -6, 46], [1, 0, -5, 46], [1, 0, -4, 46], [1, 0, -3, 46], [1, 0, -2, 46], [1, 0, -1, 46], [1, 0, 0, 46], [1, 0, 1, 46], [1, 0, 2, 46], [1, 0, 3, 46], [1, 0, 4, 46], [1, 0, 5, 46], [1, 0, 6, 46], [1, 0, 7, 46], [1, 0, 8, 46], [1, 0, 9, 46], [1, 0, 10, 46], [1, 1, -9, 46], [1, 1, -8, 46], [1, 1, -7, 46], [1, 1, -6, 46], [1, 1, -5, 46], [1, 1, -4, 46], [1, 1, -3, 46], [1, 1, -2, 46], [1, 1, -1, 46], [1, 1, 0, 46], [1, 1, 1, 46], [1, 1, 2, 46], [1, 1, 3, 46], [1, 1, 4, 46], [1, 1, 5, 46], [1, 1, 6, 46], [1, 1, 7, 46], [1, 1, 8, 46], [1, 1, 9, 46], [1, 1, 10, 46], [1, 2, -9, 46], [1, 2, -8, 46], [1, 2, -7, 46], [1, 2, -6, 46], [1, 2, -5, 46], [1, 2, -4, 46], [1, 2, -3, 46], [1, 2, -2, 46], [1, 2, -1, 46], [1, 2, 0, 46], [1, 2, 1, 46], [1, 2, 2, 46], [1, 2, 3, 46], [1, 2, 4, 46], [1, 2, 5, 46], [1, 2, 6, 46], [1, 2, 7, 46], [1, 2, 8, 46], [1, 2, 9, 46], [1, 2, 10, 46], [2, 0, -9, 46], [2, 0, -8, 46], [2, 0, -7, 46], [2, 0, -6, 46], [2, 0, -5, 46], [2, 0, -4, 46], [2, 0, -3, 46], [2, 0, -2, 46], [2, 0, -1, 46], [2, 0, 0, 46], [2, 0, 1, 46], [2, 0, 2, 46], [2, 0, 3, 46], [2, 0, 4, 46], [2, 0, 5, 46], [2, 0, 6, 46], [2, 0, 7, 46], [2, 0, 8, 46], [2, 0, 9, 46], [2, 0, 10, 46], [2, 1, -9, 46], [2, 1, -8, 46], [2, 1, -7, 46], [2, 1, -6, 46], [2, 1, -5, 46], [2, 1, -4, 46], [2, 1, -3, 46], [2, 1, -2, 46], [2, 1, -1, 46], [2, 1, 0, 46], [2, 1, 1, 46], [2, 1, 2, 46], [2, 1, 3, 46], [2, 1, 4, 46], [2, 1, 5, 46], [2, 1, 6, 46], [2, 1, 7, 46], [2, 1, 8, 46], [2, 1, 9, 46], [2, 1, 10, 46], [2, 2, -9, 46], [2, 2, -8, 46], [2, 2, -7, 46], [2, 2, -6, 46], [2, 2, -5, 46], [2, 2, -4, 46], [2, 2, -3, 46], [2, 2, -2, 46], [2, 2, -1, 46], [2, 2, 0, 46], [2, 2, 1, 46], [2, 2, 2, 46], [2, 2, 3, 46], [2, 2, 4, 46], [2, 2, 5, 46], [2, 2, 6, 46], [2, 2, 7, 46], [2, 2, 8, 46], [2, 2, 9, 46], [2, 2, 10, 46], [3, 0, -9, 46], [3, 0, -8, 46], [3, 0, -7, 46], [3, 0, -6, 46], [3, 0, -5, 46], [3, 0, -4, 46], [3, 0, -3, 46], [3, 0, -2, 46], [3, 0, -1, 46], [3, 0, 0, 46], [3, 0, 1, 46], [3, 0, 2, 46], [3, 0, 3, 46], [3, 0, 4, 46], [3, 0, 5, 46], [3, 0, 6, 46], [3, 0, 7, 46], [3, 0, 8, 46], [3, 0, 9, 46], [3, 0, 10, 46], [3, 1, -9, 46], [3, 1, -8, 46], [3, 1, -7, 46], [3, 1, -6, 46], [3, 1, -5, 46], [3, 1, -4, 46], [3, 1, -3, 46], [3, 1, -2, 46], [3, 1, -1, 46], [3, 1, 0, 46], [3, 1, 1, 46], [3, 1, 2, 46], [3, 1, 3, 46], [3, 1, 4, 46], [3, 1, 5, 46], [3, 1, 6, 46], [3, 1, 7, 46], [3, 1, 8, 46], [3, 1, 9, 46], [3, 1, 10, 46], [3, 2, -9, 46], [3, 2, -8, 46], [3, 2, -7, 46], [3, 2, -6, 46], [3, 2, -5, 46], [3, 2, -4, 46], [3, 2, -3, 46], [3, 2, -2, 46], [3, 2, -1, 46], [3, 2, 0, 46], [3, 2, 1, 46], [3, 2, 2, 46], [3, 2, 3, 46], [3, 2, 4, 46], [3, 2, 5, 46], [3, 2, 6, 46], [3, 2, 7, 46], [3, 2, 8, 46], [3, 2, 9, 46], [3, 2, 10, 46], [4, 0, -9, 46], [4, 0, -8, 46], [4, 0, -7, 46], [4, 0, -6, 46], [4, 0, -5, 46], [4, 0, -4, 46], [4, 0, -3, 46], [4, 0, -2, 46], [4, 0, -1, 46], [4, 0, 0, 46], [4, 0, 1, 46], [4, 0, 2, 46], [4, 0, 3, 46], [4, 0, 4, 46], [4, 0, 5, 46], [4, 0, 6, 46], [4, 0, 7, 46], [4, 0, 8, 46], [4, 0, 9, 46], [4, 0, 10, 46], [4, 1, -9, 46], [4, 1, -8, 46], [4, 1, -7, 46], [4, 1, -6, 46], [4, 1, -5, 46], [4, 1, -4, 46], [4, 1, -3, 46], [4, 1, -2, 46], [4, 1, -1, 46], [4, 1, 0, 46], [4, 1, 1, 46], [4, 1, 2, 46], [4, 1, 3, 46], [4, 1, 4, 46], [4, 1, 5, 46], [4, 1, 6, 46], [4, 1, 7, 46], [4, 1, 8, 46], [4, 1, 9, 46], [4, 1, 10, 46], [4, 2, -9, 46], [4, 2, -8, 46], [4, 2, -7, 46], [4, 2, -6, 46], [4, 2, -5, 46], [4, 2, -4, 46], [4, 2, -3, 46], [4, 2, -2, 46], [4, 2, -1, 46], [4, 2, 0, 46], [4, 2, 1, 46], [4, 2, 2, 46], [4, 2, 3, 46], [4, 2, 4, 46], [4, 2, 5, 46], [4, 2, 6, 46], [4, 2, 7, 46], [4, 2, 8, 46], [4, 2, 9, 46], [4, 2, 10, 46], [5, 0, -9, 46], [5, 0, -8, 46], [5, 0, -7, 46], [5, 0, -6, 46], [5, 0, -5, 46], [5, 0, -4, 46], [5, 0, -3, 46], [5, 0, -2, 46], [5, 0, -1, 46], [5, 0, 0, 46], [5, 0, 1, 46], [5, 0, 2, 46], [5, 0, 3, 46], [5, 0, 4, 46], [5, 0, 5, 46], [5, 0, 6, 46], [5, 0, 7, 46], [5, 0, 8, 46], [5, 0, 9, 46], [5, 0, 10, 46], [5, 1, -9, 46], [5, 1, -8, 46], [5, 1, -7, 46], [5, 1, -6, 46], [5, 1, -5, 46], [5, 1, -4, 46], [5, 1, -3, 46], [5, 1, -2, 46], [5, 1, -1, 46], [5, 1, 0, 46], [5, 1, 1, 46], [5, 1, 2, 46], [5, 1, 3, 46], [5, 1, 4, 46], [5, 1, 5, 46], [5, 1, 6, 46], [5, 1, 7, 46], [5, 1, 8, 46], [5, 1, 9, 46], [5, 1, 10, 46], [5, 2, -9, 46], [5, 2, -8, 46], [5, 2, -7, 46], [5, 2, -6, 46], [5, 2, -5, 46], [5, 2, -4, 46], [5, 2, -3, 46], [5, 2, -2, 46], [5, 2, -1, 46], [5, 2, 0, 46], [5, 2, 1, 46], [5, 2, 2, 46], [5, 2, 3, 46], [5, 2, 4, 46], [5, 2, 5, 46], [5, 2, 6, 46], [5, 2, 7, 46], [5, 2, 8, 46], [5, 2, 9, 46], [5, 2, 10, 46], [6, 0, -9, 46], [6, 0, -8, 46], [6, 0, -7, 46], [6, 0, -6, 46], [6, 0, -5, 46], [6, 0, -4, 46], [6, 0, -3, 46], [6, 0, -2, 46], [6, 0, -1, 46], [6, 0, 0, 46], [6, 0, 1, 46], [6, 0, 2, 46], [6, 0, 3, 46], [6, 0, 4, 46], [6, 0, 5, 46], [6, 0, 6, 46], [6, 0, 7, 46], [6, 0, 8, 46], [6, 0, 9, 46], [6, 0, 10, 46], [6, 1, -9, 46], [6, 1, -8, 46], [6, 1, -7, 46], [6, 1, -6, 46], [6, 1, -5, 46], [6, 1, -4, 46], [6, 1, -3, 46], [6, 1, -2, 46], [6, 1, -1, 46], [6, 1, 0, 46], [6, 1, 1, 46], [6, 1, 2, 46], [6, 1, 3, 46], [6, 1, 4, 46], [6, 1, 5, 46], [6, 1, 6, 46], [6, 1, 7, 46], [6, 1, 8, 46], [6, 1, 9, 46], [6, 1, 10, 46], [6, 2, -9, 46], [6, 2, -8, 46], [6, 2, -7, 46], [6, 2, -6, 46], [6, 2, -5, 46], [6, 2, -4, 46], [6, 2, -3, 46], [6, 2, -2, 46], [6, 2, -1, 46], [6, 2, 0, 46], [6, 2, 1, 46], [6, 2, 2, 46], [6, 2, 3, 46], [6, 2, 4, 46], [6, 2, 5, 46], [6, 2, 6, 46], [6, 2, 7, 46], [6, 2, 8, 46], [6, 2, 9, 46], [6, 2, 10, 46], [7, 0, -9, 46], [7, 0, -8, 46], [7, 0, -7, 46], [7, 0, -6, 46], [7, 0, -5, 46], [7, 0, -4, 46], [7, 0, -3, 46], [7, 0, -2, 46], [7, 0, -1, 46], [7, 0, 0, 46], [7, 0, 1, 46], [7, 0, 2, 46], [7, 0, 3, 46], [7, 0, 4, 46], [7, 0, 5, 46], [7, 0, 6, 46], [7, 0, 7, 46], [7, 0, 8, 46], [7, 0, 9, 46], [7, 0, 10, 46], [7, 1, -9, 46], [7, 1, -8, 46], [7, 1, -7, 46], [7, 1, -6, 46], [7, 1, -5, 46], [7, 1, -4, 46], [7, 1, -3, 46], [7, 1, -2, 46], [7, 1, -1, 46], [7, 1, 0, 46], [7, 1, 1, 46], [7, 1, 2, 46], [7, 1, 3, 46], [7, 1, 4, 46], [7, 1, 5, 46], [7, 1, 6, 46], [7, 1, 7, 46], [7, 1, 8, 46], [7, 1, 9, 46], [7, 1, 10, 46], [7, 2, -9, 46], [7, 2, -8, 46], [7, 2, -7, 46], [7, 2, -6, 46], [7, 2, -5, 46], [7, 2, -4, 46], [7, 2, -3, 46], [7, 2, -2, 46], [7, 2, -1, 46], [7, 2, 0, 46], [7, 2, 1, 46], [7, 2, 2, 46], [7, 2, 3, 46], [7, 2, 4, 46], [7, 2, 5, 46], [7, 2, 6, 46], [7, 2, 7, 46], [7, 2, 8, 46], [7, 2, 9, 46], [7, 2, 10, 46], [8, 0, -9, 46], [8, 0, -8, 46], [8, 0, -7, 46], [8, 0, -6, 46], [8, 0, -5, 46], [8, 0, -4, 46], [8, 0, -3, 46], [8, 0, -2, 46], [8, 0, -1, 46], [8, 0, 0, 46], [8, 0, 1, 46], [8, 0, 2, 46], [8, 0, 3, 46], [8, 0, 4, 46], [8, 0, 5, 46], [8, 0, 6, 46], [8, 0, 7, 46], [8, 0, 8, 46], [8, 0, 9, 46], [8, 0, 10, 46], [8, 1, -9, 46], [8, 1, -8, 46], [8, 1, -7, 46], [8, 1, -6, 46], [8, 1, -5, 46], [8, 1, -4, 46], [8, 1, -3, 46], [8, 1, -2, 46], [8, 1, -1, 46], [8, 1, 0, 46], [8, 1, 1, 46], [8, 1, 2, 46], [8, 1, 3, 46], [8, 1, 4, 46], [8, 1, 5, 46], [8, 1, 6, 46], [8, 1, 7, 46], [8, 1, 8, 46], [8, 1, 9, 46], [8, 1, 10, 46], [8, 2, -9, 46], [8, 2, -8, 46], [8, 2, -7, 46], [8, 2, -6, 46], [8, 2, -5, 46], [8, 2, -4, 46], [8, 2, -3, 46], [8, 2, -2, 46], [8, 2, -1, 46], [8, 2, 0, 46], [8, 2, 1, 46], [8, 2, 2, 46], [8, 2, 3, 46], [8, 2, 4, 46], [8, 2, 5, 46], [8, 2, 6, 46], [8, 2, 7, 46], [8, 2, 8, 46], [8, 2, 9, 46], [8, 2, 10, 46], [9, 0, -9, 46], [9, 0, -8, 46], [9, 0, -7, 46], [9, 0, -6, 46], [9, 0, -5, 46], [9, 0, -4, 46], [9, 0, -3, 46], [9, 0, -2, 46], [9, 0, -1, 46], [9, 0, 0, 46], [9, 0, 1, 46], [9, 0, 2, 46], [9, 0, 3, 46], [9, 0, 4, 46], [9, 0, 5, 46], [9, 0, 6, 46], [9, 0, 7, 46], [9, 0, 8, 46], [9, 0, 9, 46], [9, 0, 10, 46], [9, 1, -9, 46], [9, 1, -8, 46], [9, 1, -7, 46], [9, 1, -6, 46], [9, 1, -5, 46], [9, 1, -4, 46], [9, 1, -3, 46], [9, 1, -2, 46], [9, 1, -1, 46], [9, 1, 0, 46], [9, 1, 1, 46], [9, 1, 2, 46], [9, 1, 3, 46], [9, 1, 4, 46], [9, 1, 5, 46], [9, 1, 6, 46], [9, 1, 7, 46], [9, 1, 8, 46], [9, 1, 9, 46], [9, 1, 10, 46], [9, 2, -9, 46], [9, 2, -8, 46], [9, 2, -7, 46], [9, 2, -6, 46], [9, 2, -5, 46], [9, 2, -4, 46], [9, 2, -3, 46], [9, 2, -2, 46], [9, 2, -1, 46], [9, 2, 0, 46], [9, 2, 1, 46], [9, 2, 2, 46], [9, 2, 3, 46], [9, 2, 4, 46], [9, 2, 5, 46], [9, 2, 6, 46], [9, 2, 7, 46], [9, 2, 8, 46], [9, 2, 9, 46], [9, 2, 10, 46], [10, 0, -9, 46], [10, 0, -8, 46], [10, 0, -7, 46], [10, 0, -6, 46], [10, 0, -5, 46], [10, 0, -4, 46], [10, 0, -3, 46], [10, 0, -2, 46], [10, 0, -1, 46], [10, 0, 0, 46], [10, 0, 1, 46], [10, 0, 2, 46], [10, 0, 3, 46], [10, 0, 4, 46], [10, 0, 5, 46], [10, 0, 6, 46], [10, 0, 7, 46], [10, 0, 8, 46], [10, 0, 9, 46], [10, 0, 10, 46], [10, 1, -9, 46], [10, 1, -8, 46], [10, 1, -7, 46], [10, 1, -6, 46], [10, 1, -5, 46], [10, 1, -4, 46], [10, 1, -3, 46], [10, 1, -2, 46], [10, 1, -1, 46], [10, 1, 0, 46], [10, 1, 1, 46], [10, 1, 2, 46], [10, 1, 3, 46], [10, 1, 4, 46], [10, 1, 5, 46], [10, 1, 6, 46], [10, 1, 7, 46], [10, 1, 8, 46], [10, 1, 9, 46], [10, 1, 10, 46], [10, 2, -9, 46], [10, 2, -8, 46], [10, 2, -7, 46], [10, 2, -6, 46], [10, 2, -5, 46], [10, 2, -4, 46], [10, 2, -3, 46], [10, 2, -2, 46], [10, 2, -1, 46], [10, 2, 0, 46], [10, 2, 1, 46], [10, 2, 2, 46], [10, 2, 3, 46], [10, 2, 4, 46], [10, 2, 5, 46], [10, 2, 6, 46], [10, 2, 7, 46], [10, 2, 8, 46], [10, 2, 9, 46], [10, 2, 10, 46], [-6, 7, -2, 57], [-6, 7, -1, 57], [-6, 7, 0, 57], [-6, 7, 1, 57], [-5, 7, -2, 57], [-5, 7, 1, 57], [-4, 7, -2, 57], [-4, 7, 1, 57], [-3, 7, -2, 57], [-3, 7, -1, 57], [-3, 7, 0, 57], [-3, 7, 1, 57]]}]

    aj = VisionAnnotationJob(202201242254123, "000", example_scene, 300)
    aj.run()
