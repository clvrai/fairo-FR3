import mrp

# For the Conda environments, see README for installation instructions.
# TODO: automatic creation of these Conda environments.

mrp.process(
    name="segmentation_server",
    runtime=mrp.Conda(
        run_command=["python", "-m", "utils.mrp_wrapper"],
        use_named_env="unseen-object-clustering",
    ),
)

mrp.process(
    name="grasp_server",
    runtime=mrp.Conda(
        run_command=["python", "-m", "graspnet_baseline.mrp_wrapper"],
        use_named_env="graspnet-baseline",
    ),
)

mrp.process(
    name="cam_pub",
    runtime=mrp.Conda(
        use_named_env="polygrasp",
        run_command=["python", "-m", "polygrasp.cam_pub_sub"],
    ),
)

mrp.process(
    name="gripper_server",
    runtime=mrp.Conda(
        use_named_env="polygrasp",
        run_command=["launch_gripper.py", "gripper=robotiq_2f", "gripper.comport=/dev/ttyUSB1"],
    ),
)

mrp.main()
