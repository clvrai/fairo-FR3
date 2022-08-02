# Copyright (c) Facebook, Inc. and its affiliates.

# This source code is licensed under the MIT license found in the
# LICENSE file in the root directory of this source tree.
import logging
import mujoco
import numpy as np

from omegaconf import DictConfig

from polysim.envs import AbstractControlledEnv
from polymetis.utils.data_dir import get_full_path_to_urdf

log = logging.getLogger(__name__)


class MujocoManipulatorEnv(AbstractControlledEnv):
    def __init__(
        self,
        robot_model_cfg: DictConfig,
        gui: bool,
        use_grav_comp: bool = True,
        gravity: float = 9.81,
    ):
        self.robot_model_cfg = robot_model_cfg
        self.robot_description_path = get_full_path_to_urdf(
            self.robot_model_cfg.robot_description_path
        )

        self.robot_model = mujoco.MjModel.from_xml_path(self.robot_description_path)
        self.robot_data = mujoco.MjData(model)

        self.controlled_joints = self.robot_model_cfg.controlled_joints
        self.n_dofs = self.robot_model_cfg.num_dofs
        assert len(self.controlled_joints) == self.n_dofs
        self.ee_link_idx = self.robot_model_cfg.ee_link_idx
        self.ee_link_name = self.robot_model_cfg.ee_link_name
        self.rest_pose = self.robot_model_cfg.rest_pose
        self.joint_limits_low = np.array(self.robot_model_cfg.joint_limits_low)
        self.joint_limits_high = np.array(self.robot_model_cfg.joint_limits_high)
        if self.robot_model_cfg.joint_damping is None:
            self.joint_damping = None
        else:
            self.joint_damping = np.array(self.robot_model_cfg.joint_damping)
        if self.robot_model_cfg.torque_limits is None:
            self.torque_limits = np.inf * np.ones(self.n_dofs)
        else:
            self.torque_limits = np.array(self.robot_model_cfg.torque_limits)

        self.prev_torques_commanded = np.zeros(self.n_dofs)
        self.prev_torques_applied = np.zeros(self.n_dofs)
        self.prev_torques_measured = np.zeros(self.n_dofs)
        self.prev_torques_external = np.zeros(self.n_dofs)

    def reset(self):
        """Reset the environment."""
        mujoco.mj_resetData(self.robot_model, self.robot_data)

    def get_num_dofs(self) -> int:
        """Get the number of degrees of freedom for controlling the simulation.

        Returns:
            int: Number of control input dimensions
        """
        return self.n_dofs

    def get_current_joint_pos_vel(self) -> tuple[np.ndarray, np.ndarray]:
        """
        Returns:
            np.ndarray: Joint positions
            np.ndarray: Joint velocities
        """
        return (
            self.robot_data.qpos,
            self.robot_data.qvel,
        )

    def get_current_joint_torques(
        self,
    ) -> tuple[np.ndarray, np.ndarray, np.ndarray, np.ndarray]:
        """
        Returns:
            np.ndarray: Torques received from apply_joint_torques
            np.ndarray: Torques sent to robot (e.g. after clipping)
            np.ndarray: Torques generated by the actuators (e.g. after grav comp)
            np.ndarray: Torques exerted onto the robot
        """
        return (
            self.prev_torques_commanded,
            self.prev_torques_applied,
            self.robot_data.actuator_force,
            self.prev_torques_external,  # zeros
        )

    def apply_joint_torques(self, torques: np.ndarray):
        """
        input:
            np.ndarray: Desired torques
        Returns:
            np.ndarray: final applied torque
        """
        self.prev_torques_commanded = torques
        applied_torques = np.clip(torque, -self.torque_limits, self.torque_limits)
        if self.use_grav_comp:
            joint_cur_pos = self.get_current_joint_pos()
            grav_comp_torques = self.compute_inverse_dynamics(
                joint_pos=joint_cur_pos,
                joint_vel=[0] * self.n_dofs,
                joint_acc=[0] * self.n_dofs,
            )  # zero vel + acc to find gravity
            applied_torque += grav_comp_torques
        self.prev_torques_applied = applied_torques.copy()
        self.robot_data.ctrl = applied_torques
        mujoco.mj_step(self.robot_model, self.robot_data)
        return applied_torques
