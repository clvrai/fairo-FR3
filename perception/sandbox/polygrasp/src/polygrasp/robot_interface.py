"""polymetis.RobotInterface combined with GripperInterface, with an additional `grasp` method."""

import time
import numpy as np
import logging
from scipy.spatial.transform import Rotation as R
import torch

import hydra
import graspnetAPI
import polymetis

import ikpy.chain
import tempfile

log = logging.getLogger(__name__)

threshold_dist_to_ref_angle = 1.4


def compute_des_pose(best_grasp):
    """Convert between GraspNet coordinates to robot coordinates."""

    # Grasp point
    grasp_point = best_grasp.translation

    # Compute plane of rotation through three orthogonal vectors on plane of rotation
    grasp_approach_delta = best_grasp.rotation_matrix @ np.array([-0.3, 0.0, 0])
    grasp_approach_delta_plus = best_grasp.rotation_matrix @ np.array([-0.3, 0.1, 0])
    grasp_approach_delta_minus = best_grasp.rotation_matrix @ np.array([-0.3, -0.1, 0])
    bx = -grasp_approach_delta
    by = grasp_approach_delta_plus - grasp_approach_delta_minus
    bx = bx / np.linalg.norm(bx)
    by = by / np.linalg.norm(by)
    bz = np.cross(bx, by)
    plane_rot = R.from_matrix(np.vstack([bx, by, bz]).T)

    # Convert between GraspNet neutral orientation to robot neutral orientation
    des_ori = plane_rot * R.from_euler("y", 90, degrees=True)
    des_ori_quat = des_ori.as_quat()

    return grasp_point, grasp_approach_delta, des_ori_quat


def grasp_to_pose(grasp: graspnetAPI.Grasp):
    return grasp.translation, R.from_matrix(grasp.rotation_matrix).as_quat()


def compute_quat_dist(a, b):
    return torch.acos((2 * (a * b).sum() ** 2 - 1).clip(-1, 1))


def min_dist_grasp(default_ee_quat, grasps):
    """Find the grasp with minimum orientation distance to the reference grasp"""
    with torch.no_grad():
        rots_as_quat = [
            torch.Tensor(R.from_matrix(grasp.rotation_matrix).as_quat())
            for grasp in grasps
        ]
        dists = [compute_quat_dist(rot, default_ee_quat) for rot in rots_as_quat]
        i = torch.argmin(torch.Tensor(dists)).item()
    log.info(f"Grasp {i} has angle {dists[i]} from reference.")
    return grasps[i], i


def min_dist_grasp_no_z(default_ee_quat, grasps):
    """
    Find the grasp with minimum orientation distance to the reference grasp
    disregarding orientation about z axis
    """
    with torch.no_grad():
        rots_as_R = [R.from_quat(compute_des_pose(grasp)[2]) for grasp in grasps]
        default_r = R.from_quat(default_ee_quat)
        dists = [
            np.linalg.norm((rot.inv() * default_r).as_rotvec()[:2]) for rot in rots_as_R
        ]
        i = torch.argmin(torch.Tensor(dists)).item()
    print(f"Grasp {i} has angle {dists[i]} from reference.")
    log.info(f"Grasp {i} has angle {dists[i]} from reference.")
    return grasps[i], i, dists


class GraspingRobotInterface(polymetis.RobotInterface):
    def __init__(
        self,
        gripper: polymetis.GripperInterface,
        k_approach=1.5,
        k_grasp=0.72,
        gripper_max_width=0.085,
        # ikpy params:
        base_elements=["panda_link0"],
        soft_limits=[
            (-2.70, 2.70),
            (-1.56, 1.56),
            (-2.7, 2.7),
            (-2.87, -0.07),
            (-2.7, 2.7),
            (-0.02, 3.55),
            (-2.7, 2.7),
        ],
        # soft_limits=[
        #     (-3.1, 0.5),
        #     (-1.5, 3.1),
        #     (-3.1, 3.1),
        #     (-3.1, 1.5),
        #     (-2.7, 2.7),
        #     (-3.1, 3.55),
        #     (-2.7, 2.7),
        # ],
        *args,
        **kwargs,
    ):
        super().__init__(*args, **kwargs)
        self.gripper = hydra.utils.instantiate(gripper)

        self.default_ee_quat = torch.Tensor([1, 0, 0, 0])
        self.k_approach = k_approach
        self.k_grasp = k_grasp
        self.gripper_max_width = gripper_max_width

        with tempfile.NamedTemporaryFile(mode="w+") as f:
            f.write(self.metadata.urdf_file)
            f.seek(0)
            self.robot_model_ikpy = ikpy.chain.Chain.from_urdf_file(
                f.name,
                base_elements=base_elements,
            )
        for i in range(len(soft_limits)):
            self.robot_model_ikpy.links[i + 1].bounds = soft_limits[i]

    def ik(self, position, orientation=None):
        curr_joint_pos = [0] + self.get_joint_positions().numpy().tolist() + [0]
        des_homog_transform = np.eye(4)
        if orientation is not None:
            des_homog_transform[:3, :3] = R.from_quat(orientation).as_matrix()
        des_homog_transform[:3, 3] = position
        try:
            joint_pos_ikpy = self.robot_model_ikpy.inverse_kinematics_frame(
                target=des_homog_transform,
                orientation_mode="all",
                no_position=False,
                initial_position=curr_joint_pos,
            )
            return joint_pos_ikpy[1:-1]
        except ValueError as e:
            print(f"Can't find IK solution! {e}")
            return None

    def gripper_open(self):
        self.gripper.goto(1, 1, 1)

    def gripper_close(self):
        self.gripper.goto(0, 1, 1)

    def move_until_success(
        self, position, orientation, time_to_go, max_attempts=2, success_dist=0.05
    ):
        states = []
        for _ in range(max_attempts):
            joint_pos = self.ik(position, orientation)
            if joint_pos is None:
                print('going home as no solution was found for {position} with IK')
                self.go_home()
            states += self.move_to_joint_positions(joint_pos, time_to_go=time_to_go)
            curr_ee_pos, curr_ee_ori = self.get_ee_pose()

            xyz_diff = torch.linalg.norm(curr_ee_pos - position)
            ori_diff = (
                R.from_quat(curr_ee_ori).inv() * R.from_quat(orientation)
            ).magnitude()
            log.info(f"Dist to goal: xyz diff {xyz_diff}, ori diff {ori_diff}")

            if (
                states
                and states[-1].prev_command_successful
                and xyz_diff < success_dist
                and ori_diff < 0.2
            ):
                break
        return states, curr_ee_pos

    def check_feasibility(self, point: np.ndarray):
        return self.ik(point) is not None

    def select_grasp(
        self, grasps: graspnetAPI.GraspGroup, num_grasp_choices=20
    ) -> graspnetAPI.Grasp:
        with torch.no_grad():
            feasible_i = []
            for i, grasp in enumerate(grasps):
                print(f"checking feasibility {i}/{len(grasps)}")

                if grasp.width > self.gripper_max_width:
                    continue

                grasp_point, grasp_approach_delta, des_ori_quat = compute_des_pose(
                    grasp
                )
                # Vidhi: check with Yixin
                # breakpoint()
                point_a = grasp_point + self.k_approach * grasp_approach_delta
                point_b = grasp_point + self.k_grasp * grasp_approach_delta
                
                #Vidhi : maybe change z of these points

                if self.check_feasibility(point_a) and self.check_feasibility(point_b):
                    feasible_i.append(i)

                # if len(feasible_i) == num_grasp_choices:
                #     if i >= num_grasp_choices:
                #         print(
                #             f"Kinematically filtered {i + 1 - num_grasp_choices} grasps"
                #             " to get 5 feasible positions"
                #         )
                #     break

            # Choose the grasp closest to the neutral position
            filtered_grasps = grasps[feasible_i]
            if len(filtered_grasps) > 1:
                grasp, i, dists = min_dist_grasp_no_z(self.default_ee_quat, filtered_grasps)
                if dists[i] < threshold_dist_to_ref_angle:
                    log.info(f"Closest grasp to ee ori, within top {len(grasps)}: {i + 1}")
                    # breakpoint()
                    return grasp, filtered_grasps
                else:
                    print('angle too big, will hit the table!!!')

            return None, filtered_grasps
            

    def grasp(
        self,
        grasp: graspnetAPI.Grasp,
        time_to_go=3,
        gripper_width_success_threshold=0.00095,
    ):
        states = []
        grasp_point, grasp_approach_delta, des_ori_quat = compute_des_pose(grasp)

        self.gripper_open()
        approach_pos = torch.Tensor(grasp_point + grasp_approach_delta * self.k_approach)
        states += self.move_until_success(
            position=approach_pos,
            orientation=torch.Tensor(des_ori_quat),
            time_to_go=time_to_go,
        )
        curr_ee_pos, curr_ee_ori = self.get_ee_pose()
        if torch.linalg.norm(curr_ee_pos - approach_pos) > 15e-2:
            success = False
            return states, success

        # if states[-1]
        # except:
        #     print('Exception : failed to execute the grasp approach')
        #     success = False 
        #     return states, success
        grip_ee_pos = torch.Tensor(grasp_point + grasp_approach_delta * self.k_grasp) - torch.Tensor([0, 0, 0.02])


        states += self.move_until_success(
            position=grip_ee_pos,
            orientation=torch.Tensor(des_ori_quat),
            time_to_go=time_to_go,
        )

        if torch.linalg.norm(curr_ee_pos - grip_ee_pos) > 15e-2:
            success = False
            return states, success 
        
        self.gripper_close()

        log.info(f"Waiting for gripper to close...")
        while self.gripper.get_state().is_moving:
            time.sleep(0.2)

        gripper_state = self.gripper.get_state()
        width = gripper_state.width
        log.info(f"Gripper width: {width}")

        success = width > gripper_width_success_threshold

        return states, success
