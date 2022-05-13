"""polymetis.RobotInterface combined with GripperInterface, with an additional `grasp` method."""

import time
import numpy as np
import logging
from scipy.spatial.transform import Rotation as R
import torch

import hydra
import graspnetAPI
import polymetis

log = logging.getLogger(__name__)


def compute_des_pose(best_grasp):
    grasp_point = best_grasp.translation

    grasp_approach_delta = best_grasp.rotation_matrix @ np.array([-0.3, 0.0, 0])
    grasp_approach_delta_plus = best_grasp.rotation_matrix @ np.array([-0.3, 0.1, 0])
    grasp_approach_delta_minus = best_grasp.rotation_matrix @ np.array([-0.3, -0.1, 0])
    bx = -grasp_approach_delta
    by = grasp_approach_delta_plus - grasp_approach_delta_minus
    bx = bx / np.linalg.norm(bx)
    by = by / np.linalg.norm(by)
    bz = np.cross(bx, by)
    plane_rot = R.from_matrix(np.vstack([bx, by, bz]).T)

    des_ori = plane_rot * R.from_euler("x", 90, degrees=True) * R.from_euler("y", 90, degrees=True)
    des_ori_quat = des_ori.as_quat()

    return grasp_point, grasp_approach_delta, des_ori_quat


def grasp_to_pose(grasp: graspnetAPI.Grasp):
    return grasp.translation, R.from_matrix(grasp.rotation_matrix).as_quat()


def compute_quat_dist(a, b):
    return torch.acos((2 * (a * b).sum() ** 2 - 1).clip(-1, 1))


def min_dist_grasp(default_ee_pose, grasps):
    with torch.no_grad():
        rots_as_quat = [
            torch.Tensor(R.from_matrix(grasp.rotation_matrix).as_quat()) for grasp in grasps
        ]
        dists = [compute_quat_dist(rot, default_ee_pose) for rot in rots_as_quat]
        i = torch.argmin(torch.Tensor(dists)).item()
    log.info(f"Grasp {i} has angle {dists[i]} from reference.")
    return grasps[i], i


class GraspingRobotInterface(polymetis.RobotInterface):
    def __init__(
        self, gripper: polymetis.GripperInterface, k_approach=1.5, k_grasp=0.72, *args, **kwargs
    ):
        super().__init__(*args, **kwargs)
        self.gripper = hydra.utils.instantiate(gripper)

        self.default_ee_pose = torch.Tensor([0.9418, 0.3289, -0.0368, -0.0592])
        self.k_approach = k_approach
        self.k_grasp = k_grasp

    def gripper_open(self):
        self.gripper.goto(1, 1, 1)

    def gripper_close(self):
        self.gripper.goto(0, 1, 1)

    def move_until_success(
        self, position, orientation, time_to_go, max_attempts=5, success_dist=0.2
    ):
        states = []
        for _ in range(max_attempts):
            states += self.move_to_ee_pose(
                position=position, orientation=orientation, time_to_go=time_to_go
            )
            curr_ee_pos, curr_ee_ori = self.get_ee_pose()

            log.info(f"Dist to goal: {torch.linalg.norm(curr_ee_pos - position)}")

            if (
                states[-1].prev_command_successful
                and torch.linalg.norm(curr_ee_pos - position) < success_dist
            ):  # TODO: orientation diff
                break
        return states

    def select_grasp(self, grasps: graspnetAPI.GraspGroup) -> graspnetAPI.Grasp:
        with torch.no_grad():
            feasible_i = []
            for i, grasp in enumerate(grasps):
                grasp_point, grasp_approach_delta, des_ori_quat = compute_des_pose(grasp)
                point_a = grasp_point + self.k_approach * grasp_approach_delta
                point_b = grasp_point + self.k_grasp * grasp_approach_delta

                def check_feasibility(point):
                    ik_sol = self.robot_model.inverse_kinematics(
                        torch.Tensor(point), torch.Tensor(des_ori_quat)
                    )
                    ee_pos, ee_quat = self.robot_model.forward_kinematics(ik_sol)
                    return torch.linalg.norm(ee_pos - point) < 0.001

                if check_feasibility(point_a) and check_feasibility(point_b):
                    feasible_i.append(i)

            if len(feasible_i) < len(grasps):
                log.warning(
                    f"Filtered out {len(grasps) - len(feasible_i)}/{len(grasps)} grasps due to kinematic infeasibility."
                )

            # Choose the grasp closest to the neutral position
            grasp, i = min_dist_grasp(self.default_ee_pose, grasps[feasible_i][:5])
            log.info(f"Closest grasp to ee ori, within top 5: {i + 1}")
            return grasp

    def grasp(
        self,
        grasp: graspnetAPI.Grasp,
        time_to_go=3,
        offset=np.array([0.0, 0.0, 0.0]),
        gripper_width_success_threshold=0.0005,
    ):
        states = []
        grasp_point, grasp_approach_delta, des_ori_quat = compute_des_pose(grasp)

        self.gripper_open()
        states += self.move_until_success(
            position=torch.Tensor(grasp_point + grasp_approach_delta * self.k_approach + offset),
            orientation=torch.Tensor(des_ori_quat),
            time_to_go=time_to_go,
        )

        grip_ee_pos = torch.Tensor(grasp_point + grasp_approach_delta * self.k_grasp + offset)

        states += self.move_until_success(
            position=grip_ee_pos, orientation=torch.Tensor(des_ori_quat), time_to_go=time_to_go
        )
        self.gripper_close()

        log.info(f"Waiting for gripper to close...")
        time.sleep(1.5)

        gripper_state = self.gripper.get_state()
        width = gripper_state.width
        log.info(f"Gripper width: {width}")

        success = width > gripper_width_success_threshold

        return states, success
