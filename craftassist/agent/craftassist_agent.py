"""
Copyright (c) Facebook, Inc. and its affiliates.
"""
import os
import sys
import logging
import faulthandler
import signal
import random
import sentry_sdk
import time
from multiprocessing import set_start_method
from collections import namedtuple
import subprocess

import default_behaviors
import heuristic_perception
import inventory
import mc_memory
import rotation

CA_ROOT = os.path.join(os.path.dirname(__file__), "..")
BASE_AGENT_ROOT = os.path.join(CA_ROOT, "..")
sys.path.append(BASE_AGENT_ROOT)

import dldashboard

if __name__ == "__main__":
    # this line has to go before any imports that contain @sio.on functions
    # or else, those @sio.on calls become no-ops
    print("starting dashboard...")
    dldashboard.start()

from base_agent.nsp_dialogue_manager import NSPDialogueManager
from base_agent.base_util import Pos, Look
from base_agent.loco_mc_agent import LocoMCAgent
from base_agent.memory_nodes import PlayerNode
from base_agent.argument_parser import ArgumentParser
from craftassist.agent.dialogue_objects import MCBotCapabilities, MCGetMemoryHandler, PutMemoryHandler, MCInterpreter
from craftassist.agent.low_level_perception import LowLevelMCPerception
from craftassist.agent.mc_agent import Agent as MCAgent
from dlevent import sio
from craftassist.agent.mc_util import cluster_areas, MCTime
from voxel_models.subcomponent_classifier import SubcomponentClassifierWrapper
from voxel_models.geoscorer import Geoscorer

faulthandler.register(signal.SIGUSR1)

random.seed(0)
log_formatter = logging.Formatter(
    "%(asctime)s [%(filename)s:%(lineno)s - %(funcName)s() %(levelname)s]: %(message)s"
)
logging.getLogger().handlers.clear()

sentry_sdk.init()  # enabled if SENTRY_DSN set in env
DEFAULT_BEHAVIOUR_TIMEOUT = 20
DEFAULT_FRAME = "SPEAKER"
Player = namedtuple("Player", "entityId, name, pos, look, mainHand")
Item = namedtuple("Item", "id, meta")


class CraftAssistAgent(LocoMCAgent):
    default_frame = DEFAULT_FRAME
    coordinate_transforms = rotation

    def __init__(self, opts):
        super(CraftAssistAgent, self).__init__(opts)
        self.no_default_behavior = opts.no_default_behavior
        self.point_targets = []
        self.last_chat_time = 0
        # areas must be perceived at each step
        # List of tuple (XYZ, radius), each defines a cube
        self.areas_to_perceive = []
        self.add_self_memory_node()
        self.init_inventory()
        self.init_event_handlers()

        # list of (prob, default function) pairs
        self.visible_defaults = [
            (0.001, default_behaviors.build_random_shape),
            (0.005, default_behaviors.come_to_player),
        ]
        self.perceive_on_chat = True

    def get_chats(self):
        """This function is a wrapper around self.cagent.get_incoming_chats and adds a new
        chat self.dashboard_chat which is set by the dashboard."""
        all_chats = self.cagent.get_incoming_chats()
        updated_chats = []
        if self.dashboard_chat:
            updated_chats.append(self.dashboard_chat)
            self.dashboard_chat = None
        updated_chats.extend(all_chats)
        return updated_chats

    def get_all_players(self):
        """This function is a wrapper around self.cagent.get_other_players and adds a new
        player called "dashboard" if it doesn't already exist."""
        all_players = self.cagent.get_other_players()
        updated_players = all_players
        player_exists = False
        for player in all_players:
            if player.name == "dashboard":
                player_exists = True
        if not player_exists:
            newPlayer = Player(
                12345678, "dashboard", Pos(0.0, 64.0, 0.0), Look(0.0, 0.0), Item(0, 0)
            )
            updated_players.append(newPlayer)
        return updated_players

    def get_all_player_line_of_sight(self, player_struct):
        """return a fixed value for "dashboard" player"""
        if isinstance(player_struct, Player):
            return Pos(-1, 63, 14)
        return self.cagent.get_player_line_of_sight(player_struct)

    def init_event_handlers(self):
        """Handle the socket events"""
        super().init_event_handlers()

<<<<<<< HEAD
=======
        @sio.on("sendCommandToAgent")
        def send_text_command_to_agent(sid, command):
            """Add the command to agent's incoming chats list and
            send back the parse.
            Args:
                command: The input text command from dashboard player
            Returns:
                return back a socket emit with parse of command and success status
            """
            logging.info("in send_text_command_to_agent, got the command: %r" % (command))
            agent_chat = (
                "<dashboard> " + command
            )  # the chat is coming from a player called "dashboard"
            self.dashboard_chat = agent_chat
            dialogue_manager = self.dialogue_manager
            # send back the dictionary
            try:
                x = dialogue_manager.get_logical_form(s=command, model=dialogue_manager.model)
                logging.info("logical form is : %r" % (x))
                payload = {"status": "Sent successfully", "chat": command, "chatResponse": x}
            except:
                logging.info("error in sending chat")
                payload = {"status": "Error in sending chat", "chat": command, "chatResponse": {}}
            sio.emit("setChatResponse", payload)
        
        @sio.on("getVoxelWorldInitialState")
        def setup_agent_initial_state(sid):
            MAX_RADIUS = 50
            logging.info("in setup_world_initial_state")
            agent_pos = self.get_player().pos
            x, y, z = round(agent_pos.x), round(agent_pos.y), round(agent_pos.z)
            origin = (x-MAX_RADIUS, y-MAX_RADIUS, z-MAX_RADIUS)
            yzxb = self.get_blocks(x-MAX_RADIUS, x+MAX_RADIUS, y-MAX_RADIUS, y+MAX_RADIUS, z-MAX_RADIUS, z+MAX_RADIUS)
            blocks = npy_to_blocks_list(yzxb, origin=origin)
            blocks = [((int(xyz[0]), int(xyz[1]), int(xyz[2])), (int(idm[0]), int(idm[1])))for xyz, idm in blocks]
            payload = {
                "status": "setupWorldInitialState",
                "world_state": {
                    "agent": {
                        "name": "agent",
                        "x": float(agent_pos.x),
                        "y": float(agent_pos.y),
                        "z": float(agent_pos.z),
                    },
                    "block": blocks
                },
            }
            sio.emit("setVoxelWorldInitialState", payload)

>>>>>>> 15da213... Added backend changes
    def init_inventory(self):
        """Initialize the agent's inventory"""
        self.inventory = inventory.Inventory()
        logging.info("Initialized agent inventory")

    def init_memory(self):
        """Intialize the agent memory and logging."""
        self.memory = mc_memory.MCAgentMemory(
            db_file=os.environ.get("DB_FILE", ":memory:"),
            db_log_path="agent_memory.{}.log".format(self.name),
            agent_time=MCTime(self.get_world_time),
        )
        file_log_handler = logging.FileHandler("agent.{}.log".format(self.name))
        file_log_handler.setFormatter(log_formatter)
        logging.getLogger().addHandler(file_log_handler)
        logging.info("Initialized agent memory")

    def init_perception(self):
        """Initialize perception modules"""
        self.perception_modules = {}
        self.perception_modules["low_level"] = LowLevelMCPerception(self)
        self.perception_modules["heuristic"] = heuristic_perception.PerceptionWrapper(self)
        # set up the SubComponentClassifier model
        if os.path.isfile(self.opts.semseg_model_path):
            self.perception_modules["semseg"] = SubcomponentClassifierWrapper(
                self, self.opts.semseg_model_path
            )

        self.on_demand_perception = {}
        # set up the Geoscorer model
        self.on_demand_perception["geoscorer"] = (
            Geoscorer(merger_model_path=self.opts.geoscorer_model_path)
            if os.path.isfile(self.opts.geoscorer_model_path)
            else None
        )
        self.on_demand_perception["check_inside"] = heuristic_perception.check_inside

    def init_controller(self):
        """Initialize all controllers"""
        dialogue_object_classes = {}
        dialogue_object_classes["bot_capabilities"] = MCBotCapabilities
        dialogue_object_classes["interpreter"] = MCInterpreter
        dialogue_object_classes["get_memory"] = MCGetMemoryHandler
        dialogue_object_classes["put_memory"] = PutMemoryHandler
        self.dialogue_manager = NSPDialogueManager(self, dialogue_object_classes, self.opts)

    def perceive(self, force=False):
        self.areas_to_perceive = cluster_areas(self.areas_to_perceive)
        for v in self.perception_modules.values():
            v.perceive(force=force)
        self.areas_to_perceive = []
        self.update_dashboard_world()

    def get_time(self):
        """round to 100th of second, return as
        n hundreth of seconds since agent init.
        Returns:
            Current time in the world.
        """
        return self.memory.get_time()

    def get_world_time(self):
        """MC time is based on ticks, where 20 ticks happen every second.
        There are 24000 ticks in a day, making Minecraft days exactly 20 minutes long.
        The time of day in MC is based on the timestamp modulo 24000 (default).
        0 is sunrise, 6000 is noon, 12000 is sunset, and 18000 is midnight.

        Returns:
            Time of day based on above
        """
        return self.get_time_of_day()

    def safe_get_changed_blocks(self):
        """Get all blocks that have been changed.
        Returns:
            List of changed blocks
        """
        blocks = self.cagent.get_changed_blocks()
        safe_blocks = []
        if len(self.point_targets) > 0:
            for point_target in self.point_targets:
                pt = point_target[0]
                for b in blocks:
                    x, y, z = b[0]
                    xok = x < pt[0] or x > pt[3]
                    yok = y < pt[1] or y > pt[4]
                    zok = z < pt[2] or z > pt[5]
                    if xok and yok and zok:
                        safe_blocks.append(b)
        else:
            safe_blocks = blocks
        return safe_blocks

    def point_at(self, target, sleep=None):
        """Bot pointing.

        Args:
            target: list of x1 y1 z1 x2 y2 z2, where:
                    x1 <= x2,
                    y1 <= y2,
                    z1 <= z2.
        """
        assert len(target) == 6
        self.send_chat("/point {} {} {} {} {} {}".format(*target))
        self.point_targets.append((target, time.time()))
        # sleep before the bot can take any actions
        # otherwise there might be bugs since the object is flashing
        # deal with this in the task...
        if sleep:
            time.sleep(sleep)

    def relative_head_pitch(self, angle):
        """Converts assistant's current pitch and yaw
        into a pitch and yaw relative to the angle."""
        # warning: pitch is flipped!
        new_pitch = self.get_player().look.pitch - angle
        self.set_look(self.get_player().look.yaw, new_pitch)

    def send_chat(self, chat):
        """Send chat from agent to player"""
        logging.info("Sending chat: {}".format(chat))
        self.memory.add_chat(self.memory.self_memid, chat)
        return self.cagent.send_chat(chat)

    def update_agent_pos_dashboard(self):
        agent_pos = self.get_player().pos
        payload = {
            "status": "updateVoxelWorldState",
            "world_state": {
                "agent": [{
                    "name": "agent",
                    "x": float(agent_pos.x),
                    "y": float(agent_pos.y),
                    "z": float(agent_pos.z),
                }]
            },
        }
        sio.emit("updateVoxelWorldState", payload)
    
    def update_dashboard_world(self):
        MAX_RADIUS = 2
        agent_pos = self.get_player().pos
        x, y, z = round(agent_pos.x), round(agent_pos.y), round(agent_pos.z)
        origin = (x-MAX_RADIUS, y-MAX_RADIUS, z-MAX_RADIUS)
        yzxb = self.get_blocks(x-MAX_RADIUS, x+MAX_RADIUS, y-MAX_RADIUS, y+MAX_RADIUS, z-MAX_RADIUS, z+MAX_RADIUS)

        # modified from util but keep air blocks
        def npy_to_blocks_list(npy, origin):
            import numpy as np
            blocks = []
            sy, sz, sx, _ = npy.shape
            for ry in range(sy):
                for rz in range(sz):
                    for rx in range(sx):
                        idm = tuple(npy[ry, rz, rx, :])
                        xyz = tuple(np.array([rx, ry, rz]) + origin)
                        blocks.append((xyz, idm))
            return blocks

        blocks = npy_to_blocks_list(yzxb, origin=origin)
        blocks = [((int(xyz[0]), int(xyz[1]), int(xyz[2])), (int(idm[0]), int(idm[1])))for xyz, idm in blocks]
        payload = {
            "status": "updateVoxelWorldState",
            "world_state": {
                "block": blocks
            },
        }
        sio.emit("updateVoxelWorldState", payload)


    def step_pos_x(self):
        self.cagent.step_pos_x()
        self.update_agent_pos_dashboard()

    def step_neg_x(self):
        self.cagent.step_neg_x()
        self.update_agent_pos_dashboard()

    def step_pos_y(self):
        self.cagent.step_pos_y()
        self.update_agent_pos_dashboard()

    def step_neg_y(self):
        self.cagent.step_neg_y()
        self.update_agent_pos_dashboard()

    def step_pos_z(self):
        self.cagent.step_pos_z()
        self.update_agent_pos_dashboard()

    def step_neg_z(self):
        self.cagent.step_neg_z()
        self.update_agent_pos_dashboard()

    def step_forward(self):
        self.cagent.step_forward()
        self.update_agent_pos_dashboard()


    # TODO update client so we can just loop through these
    # TODO rename things a bit- some perceptual things are here,
    #      but under current abstraction should be in init_perception
    def init_physical_interfaces(self):
        """Initializes the physical interfaces of the agent."""
        # For testing agent without cuberite server
        if self.opts.port == -1:
            return
        logging.info("Attempting to connect to port {}".format(self.opts.port))
        self.cagent = MCAgent("localhost", self.opts.port, self.name)
        logging.info("Logged in to server")
        self.dig = self.cagent.dig
        self.drop_item_stack_in_hand = self.cagent.drop_item_stack_in_hand
        self.drop_item_in_hand = self.cagent.drop_item_in_hand
        self.drop_inventory_item_stack = self.cagent.drop_inventory_item_stack
        self.set_inventory_slot = self.cagent.set_inventory_slot
        self.get_player_inventory = self.cagent.get_player_inventory
        self.get_inventory_item_count = self.cagent.get_inventory_item_count
        self.get_inventory_items_counts = self.cagent.get_inventory_items_counts
        # defined above...
        # self.send_chat = self.cagent.send_chat
        self.set_held_item = self.cagent.set_held_item
        self.step_pos_x = self.step_pos_x
        self.step_neg_x = self.step_neg_x
        self.step_pos_z = self.step_pos_z
        self.step_neg_z = self.step_neg_z
        self.step_pos_y = self.step_pos_y
        self.step_neg_y = self.step_neg_y
        self.step_forward = self.step_forward
        self.look_at = self.cagent.look_at
        self.set_look = self.cagent.set_look
        self.turn_angle = self.cagent.turn_angle
        self.turn_left = self.cagent.turn_left
        self.turn_right = self.cagent.turn_right
        self.place_block = self.cagent.place_block
        self.use_entity = self.cagent.use_entity
        self.use_item = self.cagent.use_item
        self.use_item_on_block = self.cagent.use_item_on_block
        self.is_item_stack_on_ground = self.cagent.is_item_stack_on_ground
        self.craft = self.cagent.craft
        self.get_blocks = self.cagent.get_blocks
        self.get_local_blocks = self.cagent.get_local_blocks
        self.get_incoming_chats = self.get_chats
        self.get_player = self.cagent.get_player
        self.get_mobs = self.cagent.get_mobs
        self.get_other_players = self.get_all_players
        self.get_other_player_by_name = self.cagent.get_other_player_by_name
        self.get_vision = self.cagent.get_vision
        self.get_line_of_sight = self.cagent.get_line_of_sight
        self.get_player_line_of_sight = self.get_all_player_line_of_sight
        self.get_changed_blocks = self.cagent.get_changed_blocks
        self.get_item_stacks = self.cagent.get_item_stacks
        self.get_world_age = self.cagent.get_world_age
        self.get_time_of_day = self.cagent.get_time_of_day
        self.get_item_stack = self.cagent.get_item_stack

    def add_self_memory_node(self):
        """Adds agent node into its own memory"""
        # how/when to, memory is initialized before physical interfaces...
        try:
            p = self.get_player()
        except:  # this is for test/test_agent
            return
        PlayerNode.create(self.memory, p, memid=self.memory.self_memid)


if __name__ == "__main__":
    base_path = os.path.dirname(__file__)
    parser = ArgumentParser("Minecraft", base_path)
    opts = parser.parse()

    logging.basicConfig(level=opts.log_level.upper())

    # set up stdout logging
    sh = logging.StreamHandler()
    sh.setFormatter(log_formatter)
    logger = logging.getLogger()
    logger.addHandler(sh)
    logging.info("LOG LEVEL: {}".format(logger.level))

    # Check that models and datasets are up to date
    if not opts.dev:
        rc = subprocess.call([opts.verify_hash_script_path, "craftassist"])

    set_start_method("spawn", force=True)

    sa = CraftAssistAgent(opts)
    sa.start()
