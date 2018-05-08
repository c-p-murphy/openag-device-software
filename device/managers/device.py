# Import python modules
import logging, time, json, threading, os, sys, glob

# Import django modules
from django.db.models.signals import post_save
from django.dispatch import receiver

# Import device utilities
from device.utilities.modes import Modes
from device.utilities.errors import Errors

# Import json validators
from jsonschema import validate

# Import shared memory
from device.state import State

# Import device managers
from device.managers.recipe import RecipeManager
from device.managers.event import EventManager

# Import database models
from app.models import StateModel
from app.models import EventModel
from app.models import EnvironmentModel
from app.models import SensorVariableModel
from app.models import ActuatorVariableModel
from app.models import CultivarModel
from app.models import CultivationMethodModel
from app.models import RecipeModel
from app.models import PeripheralSetupModel
from app.models import DeviceConfigModel


class DeviceManager:
    """ Manages device state machine thread that spawns child threads to run 
        recipes, read sensors, set actuators, manage control loops, sync data, 
        and manage external events. """

    # Initialize logger
    extra = {"console_name":"Device", "file_name": "device"}
    logger = logging.getLogger(__name__)
    logger = logging.LoggerAdapter(logger, extra)

    # Initialize device mode and error
    _mode = None
    _error = None

    # Initialize state object, `state` serves as shared memory between threads
    # Note: Thread should be locked whenever writing to `state` object to 
    # avoid memory corruption.
    state = State()

    # Initialize environment state dict
    state.environment = {
        "sensor": {"desired": {}, "reported": {}},
        "actuator": {"desired": {}, "reported": {}},
        "reported_sensor_stats": {
        "individual": {
                "instantaneous": {},
                "average": {}
            },
            "group": {
                "instantaneous": {},
                "average": {}
            }
        }
    }

    # Initialize recipe state dict
    state.recipe = {
        "recipe_uuid": None,
        "start_timestamp_minutes": None,
        "last_update_minute": None
    }

    # Initialize recipe object
    recipe = RecipeManager(state)

    # Intialize event object
    event = EventManager(state)
    # post_save.connect(event.process, sender=EventModel)


    # Initialize peripheral and controller managers
    peripheral_managers = None
    controller_managers = None


    def __init__(self):
        """ Initializes device. """
        self.mode = Modes.INIT
        self.error = Errors.NONE


    @property
    def mode(self):
        """ Gets mode. """
        return self._mode


    @mode.setter
    def mode(self, value):
        """ Safely updates mode in state object. """
        self._mode = value
        with threading.Lock():
            self.state.device["mode"] = value


    @property
    def commanded_mode(self):
        """ Gets commanded mode from shared state object. """
        if "commanded_mode" in self.state.device:
            return self.state.device["commanded_mode"]
        else:
            return None


    @commanded_mode.setter
    def commanded_mode(self, value):
        """ Safely updates commanded mode in state object. """
        with threading.Lock():
            self.state.device["commanded_mode"] = value


    @property
    def request(self):
        """ Gets request from shared state object. """
        if "request" in self.state.device:
            return self.state.device["request"]
        else:
            return None


    @request.setter
    def request(self, value):
        """ Safely updates request in state object. """
        with threading.Lock():
            self.state.device["request"] = value


    @property
    def response(self):
        """ Gets response from shared state object. """
        if "response" in self.state.device:
            return self.state.device["response"]
        else:
            return None


    @response.setter
    def response(self, value):
        """ Safely updates response in state object. """
        with threading.Lock():
            self.state.device["response"] = value


    @property
    def error(self):
        """ Gets device error. """
        return self._error


    @error.setter
    def error(self, value):
        """ Safely updates error in shared state. """
        self._error = value
        with threading.Lock():
            self.state.device["error"] = value


    @property
    def config_uuid(self):
        """ Gets config uuid from shared state. """
        if "config_uuid" in self.state.device:
            return self.state.device["config_uuid"]
        else:
            return None


    @config_uuid.setter
    def config_uuid(self, value):
        """ Safely updates config uuid in state. """
        with threading.Lock():
            self.state.device["config_uuid"] = value

    @property
    def commanded_config_uuid(self):
        """ Gets commanded config uuid from shared state. """
        if "commanded_config_uuid" in self.state.device:
            return self.state.device["commanded_config_uuid"]
        else:
            return None


    @commanded_config_uuid.setter
    def commanded_config_uuid(self, value):
        """ Safely updates commanded config uuid in state. """
        with threading.Lock():
            self.state.device["commanded_config_uuid"] = value


    @property
    def config_dict(self):
        """ Gets config dict for config uuid in device config table. """
        if self.config_uuid == None:
            return None
        config = DeviceConfigModel.objects.get(uuid=self.config_uuid)
        return json.loads(config.json)


    @property
    def latest_environment_timestamp(self):
        """ Gets latest environment timestamp from environment table. """
        if not EnvironmentModel.objects.all():
            return 0
        else:
            environment = EnvironmentModel.objects.latest()
            return environment.timestamp.timestamp()


    def spawn(self, delay=None):
        """ Spawns device thread. """
        self.thread = threading.Thread(target=self.run_state_machine, args=(delay,))
        self.thread.daemon = True
        self.thread.start()


    def run_state_machine(self, delay=None):
        """ Runs device state machine. """
        
        # Wait for optional delay
        if delay != None:
            time.sleep(delay)

        self.logger.info("Spawing device thread")

        # Start state machine
        self.logger.info("Started state machine")
        while True:
            if self.mode == Modes.INIT:
                self.run_init_mode()
            elif self.mode == Modes.CONFIG:
                self.run_config_mode()
            elif self.mode == Modes.SETUP:
                self.run_setup_mode()
            elif self.mode == Modes.NORMAL:
                self.run_normal_mode()
            elif self.mode == Modes.LOAD:
                self.run_load_mode()
            elif self.mode == Modes.ERROR:
                self.run_error_mode()
            elif self.mode == Modes.RESET:
                self.run_reset_mode()


    def run_init_mode(self):
        """ Runs initialization mode. Loads stored state from database then 
            transitions to CONFIG. """
        self.logger.info("Entered INIT")

        # Load local data files
        self.load_local_data_files()

        # Load stored state from database
        self.load_database_stored_state()

        # Transition to CONFIG
        self.mode = Modes.CONFIG


    def run_config_mode(self):
        """ Runs configuration mode. If device config is not set, waits for 
            config command then transitions to SETUP. """
        self.logger.info("Entered CONFIG")

        # Load an initial config during development
        # self.config_uuid = "64d72849-2e30-4a4c-8d8c-71b6b3384126" # FS1
        self.config_uuid = "5e0610fe-a488-4f70-bb7b-8fe0830fccbb" # FS1 EC
        # self.config_uuid = "8ac7744a-6fb4-4d0e-9109-bdd184a35eaf" # FS1 DO
        # self.config_uuid = "65dba26a-f9b4-48d7-a8d5-d180645cf4c6" # SMHZ1 Light
        # self.config_uuid = "7be845ec-216c-4898-9bcf-f533764b7c26" # EDU1 Light
        # self.config_uuid = "b82e43eb-7549-4fcc-aba7-b66585cb6dd1" # EDU1 SHT25


        # If device config is not set, wait for config command
        if self.config_uuid == None:
            self.logger.info("Waiting for config command")

            while True:
                if self.commanded_config_uuid != None:
                    self.config_uuid = self.commanded_config_uuid
                    self.commanded_config_uuid = None
                    break
                # Update every 100ms
                time.sleep(0.1)

        # Transition to SETUP
        self.mode = Modes.SETUP


    def run_setup_mode(self):
        """ Runs setup mode. Creates and spawns recipe, peripheral, and 
            controller threads, waits for all threads to initialize then 
            transitions to NORMAL. """
        self.logger.info("Entered SETUP")

        # Spawn recipe thread
        self.recipe.spawn()

        # Spawn event thread
        self.event.spawn()

        # Create peripheral managers and spawn threads
        self.create_peripheral_managers()
        self.spawn_peripheral_threads()

        # Create controller managers and spawn threads
        self.create_controller_managers()
        self.spawn_controller_threads()

        # Wait for all threads to initialize
        while not self.all_threads_initialized():
            time.sleep(0.2)

        # Transition to NORMAL
        self.mode = Modes.NORMAL


    def run_normal_mode(self):
        """ Runs normal operation mode. Updates device state summary and 
            stores device state in database, waits for new config command then
            transitions to CONFIG. Transitions to ERROR on error."""
        self.logger.info("Entered NORMAL")

        while True:
            # Overwrite system state in database every 100ms
            self.update_state()

            # Store environment state in every 10 minutes 
            if time.time() - self.latest_environment_timestamp > 60*10:
                self.store_environment()

            # Check for events
            request = self.request
            if self.request != None:
                self.request = None
                self.process_event(request)
            
            # Check for system error
            if self.mode == Modes.ERROR:
                break

            # Update every 100ms
            time.sleep(0.1)


    def run_load_mode(self):
        """ Runs load mode. Stops peripheral and controller threads, loads 
            config into stored state, transitions to CONFIG. """
        self.logger.info("Entered LOAD")

        # Stop peripheral and controller threads
        self.stop_peripheral_threads()
        self.stop_controller_threads()

        # Load config into stored state
        self.error = Errors.NONE

        # Transition to CONFIG
        self.mode = Modes.CONFIG

 
    def run_reset_mode(self):
        """ Runs reset mode. Kills peripheral and controller threads, clears 
            error state, then transitions to INIT. """
        self.logger.info("Entered RESET")

        # Kills peripheral and controller threads
        self.kill_peripheral_threads()
        self.kill_controller_threads()

        # Clear errors
        self.error = Errors.NONE

        # Transition to INIT
        self.mode = Modes.INIT


    def run_error_mode(self):
        """ Runs error mode. Shuts down peripheral and controller threads, 
            waits for reset signal then transitions to RESET. """
        self.logger.info("Entered ERROR")

        # Shuts down peripheral and controller threads
        self.shutdown_peripheral_threads()
        self.shutdown_controller_threads()

        # Wait for reset
        while True:
            if self.mode == Modes.RESET:
                break

            # Update every 100ms
            time.sleep(0.1) # 100ms


    def update_state(self):
        """ Updates stored state in database. If state does not exist, 
            creates it. """

        if not StateModel.objects.filter(pk=1).exists():
            StateModel.objects.create(
                id=1,
                device = json.dumps(self.state.device),
                recipe = json.dumps(self.state.recipe),
                environment = json.dumps(self.state.environment),
                peripherals = json.dumps(self.state.peripherals),
                controllers = json.dumps(self.state.controllers),
            )
        else:
            StateModel.objects.filter(pk=1).update(
                device = json.dumps(self.state.device),
                recipe = json.dumps(self.state.recipe),
                environment = json.dumps(self.state.environment),
                peripherals = json.dumps(self.state.peripherals),
                controllers = json.dumps(self.state.controllers),
            )


    def load_local_data_files(self):
        """ Loads local data files. """
        self.logger.info("Loading local data files")

        # Load files with no verification dependencies first
        self.load_sensor_variables_file()
        self.load_actuator_variables_file()
        self.load_cultivars_file()
        self.load_cultivation_methods_file()

        # Load recipe files after sensor/actuator variables, cultivars, and 
        # cultivation methods since verification depends on them
        self.load_recipe_files()

        # Load peripherals after sensor/actuator variable since verification
        # depends on them
        self.load_peripheral_setup_files()

        # Load device config after peripheral setups since verification
        # depends on  them
        self.load_device_config_files()


    def load_sensor_variables_file(self):
        """ Loads sensor variables file into database after removing all 
            existing entries. """
        self.logger.debug("Loading sensor variables file")

        # Get sensor variables
        sensor_variables = json.load(open("data/variables/sensor_variables.json"))

        # Get sensor variables schema
        sensor_variables_schema = json.load(open("data/schemas/sensor_variables.json"))

        # Validate sensor variables with schema
        validate(sensor_variables, sensor_variables_schema)

        # Delete sensor variables tables
        SensorVariableModel.objects.all().delete()

        # Create sensor variables table
        for sensor_variable in sensor_variables:
           SensorVariableModel.objects.create(json=json.dumps(sensor_variable))


    def load_actuator_variables_file(self):
        """ Loads actuator variables file into database after removing all 
            existing entries. """
        self.logger.debug("Loading actuator variables file")

        # Get sensor variables
        actuator_variables = json.load(open("data/variables/actuator_variables.json"))

        # Get sensor variables schema
        actuator_variables_schema = json.load(open("data/schemas/actuator_variables.json"))

        # Validate sensor variables with schema
        validate(actuator_variables, actuator_variables_schema)

        # Delete sensor variables tables
        ActuatorVariableModel.objects.all().delete()

        # Create sensor variables table
        for actuator_variable in actuator_variables:
           ActuatorVariableModel.objects.create(json=json.dumps(actuator_variable))


    def load_cultivars_file(self):
        """ Loads cultivars file into database after removing all 
            existing entries."""
        self.logger.debug("Loading cultivars file")

        # Get cultivars
        cultivars = json.load(open("data/cultivations/cultivars.json"))

        # Get cultivars schema
        cultivars_schema = json.load(open("data/schemas/cultivars.json"))

        # Validate cultivars with schema
        validate(cultivars, cultivars_schema)

        # Delete cultivars tables
        CultivarModel.objects.all().delete()

        # Create cultivars table
        for cultivar in cultivars:
           CultivarModel.objects.create(json=json.dumps(cultivar))


    def load_cultivation_methods_file(self):
        """ Loads cultivation methods file into database after removing all 
            existing entries. """
        self.logger.debug("Loading cultivation methods file")

        # Get cultivation methods
        cultivation_methods = json.load(open("data/cultivations/cultivation_methods.json"))

        # Get cultivation methods schema
        cultivation_methods_schema = json.load(open("data/schemas/cultivation_methods.json"))

        # Validate cultivation methods with schema
        validate(cultivation_methods, cultivation_methods_schema)

        # Delete cultivation methods tables
        CultivationMethodModel.objects.all().delete()

        # Create cultivation methods table
        for cultivation_method in cultivation_methods:
           CultivationMethodModel.objects.create(json=json.dumps(cultivation_method))


    def load_recipe_files(self):
        """ Loads recipe file into database by creating new entries if 
            nonexistant or updating existing if existant. Verification depends
            on sensor/actuator variables, cultivars, and cultivation 
            methods. """
        self.logger.debug("Loading recipe files")

        # Get recipes
        recipes = []
        for filepath in glob.glob("data/recipes/*.json"):
            recipes.append(json.load(open(filepath)))

        # Get get recipe schema
        recipe_schema = json.load(open("data/schemas/recipe.json"))

        # Validate recipes with schema
        for recipe in recipes:
            validate(recipe, recipe_schema)

        # TODO: Validate recipe variables with database variables
        # TODO: Validate recipe cycle variables with recipe environments
        # TODO: Validate recipe cultivars with database cultivars
        # TODO: Validate recipe cultivation methods with database cultivation methods

        # Create recipe entry if new or update existing
        for recipe in recipes:
            if RecipeModel.objects.filter(uuid=recipe["uuid"]).exists():
                RecipeModel.objects.update(uuid=recipe["uuid"], json=json.dumps(recipe))
            else:
                RecipeModel.objects.create(json=json.dumps(recipe))


    def load_peripheral_setup_files(self):
        """ Loads peripheral setup files from codebase into database by 
            creating new entries after deleting existing entries. Verification 
            depends on sensor/actuator variables. """
        self.logger.info("Loading peripheral setup files")

        # Get peripheral setups
        peripheral_setups = []
        for filepath in glob.glob("device/peripherals/setups/*.json"):
            self.logger.debug("Loading peripheral setup file: {}".format(filepath))
            peripheral_setups.append(json.load(open(filepath)))

        # Get get peripheral setup schema
        # TODO: Finish schema
        peripheral_setup_schema = json.load(open("data/schemas/peripheral_setup.json"))

        # Validate peripheral setups with schema
        for peripheral_setup in peripheral_setups:
            validate(peripheral_setup, peripheral_setup_schema)

        # Delete all peripheral setup entries from database
        PeripheralSetupModel.objects.all().delete()

        # TODO: Validate peripheral setup variables with database variables

        # Create peripheral setup entries in database
        for peripheral_setup in peripheral_setups:
            PeripheralSetupModel.objects.create(json=json.dumps(peripheral_setup))


    def load_device_config_files(self):
        """ Loads device config files from codebase into database by creating 
            new entries after deleting existing entries. Verification depends 
            on peripheral setups. """
        self.logger.info("Loading device config files")

        # Get devices
        device_configs = []
        for filepath in glob.glob("data/devices/*.json"):
            self.logger.debug("Loading device config file: {}".format(filepath))
            device_configs.append(json.load(open(filepath)))

        # Get get device config schema
        # TODO: Finish schema (see optional objects)
        device_config_schema = json.load(open("data/schemas/device_config.json"))

        # Validate device configs with schema
        for device_config in device_configs:
            validate(device_config, device_config_schema)

        # TODO: Validate device config with peripherals
        # TODO: Validate device config with varibles

        # Delete all device config entries from database
        DeviceConfigModel.objects.all().delete()

        # Create device config entry if new or update existing
        for device_config in device_configs:
            DeviceConfigModel.objects.create(json=json.dumps(device_config))


    def load_database_stored_state(self):
        """ Loads stored state from database if it exists. """
        self.logger.info("Loading database stored state")

        # Get stored state from database
        if not StateModel.objects.filter(pk=1).exists():
            self.logger.info("No stored state in database")
            self.config_uuid = None
            return
        stored_state = StateModel.objects.filter(pk=1).first()

        # Load device state
        stored_device_state = json.loads(stored_state.device)
        self.config_uuid = stored_device_state["config_uuid"]

        # Load recipe state
        stored_recipe_state = json.loads(stored_state.recipe)
        self.recipe.recipe_uuid = stored_recipe_state["recipe_uuid"]
        self.recipe.recipe_name = stored_recipe_state["recipe_name"]
        self.recipe.duration_minutes = stored_recipe_state["duration_minutes"]
        self.recipe.start_timestamp_minutes = stored_recipe_state["start_timestamp_minutes"]
        self.recipe.last_update_minute = stored_recipe_state["last_update_minute"]
        self.recipe.stored_mode = stored_recipe_state["mode"]

        # Load peripherals state
        stored_peripherals_state = json.loads(stored_state.peripherals)
        for peripheral_name in stored_peripherals_state:
            self.state.peripherals[peripheral_name] = {}
            if "stored" in stored_peripherals_state[peripheral_name]:
                self.state.peripherals[peripheral_name]["stored"] = stored_peripherals_state[peripheral_name]["stored"]

        # Load controllers state
        stored_controllers_state = json.loads(stored_state.controllers)
        for controller_name in stored_controllers_state:
            self.state.controllers[controller_name] = {}
            if "stored" in stored_controllers_state[controller_name]:
                self.state.controllers[controller_name]["stored"] = stored_controllers_state[controller_name]["stored"]
   
    def store_environment(self):
        """ Stores current environment state in environment table. """
        EnvironmentModel.objects.create(state=self.state.environment)


    def create_peripheral_managers(self):
        """ Creates peripheral managers. """
        self.logger.info("Creating peripheral managers")

        # Verify peripherals are configured
        if self.config_dict["peripherals"] == None:
            self.logger.info("No peripherals configured")
            return

        # Create peripheral managers
        self.peripheral_managers = {}
        for peripheral_config_dict in self.config_dict["peripherals"]:
            self.logger.debug("Creating {}".format(peripheral_config_dict["name"]))
            
            # Get peripheral setup dict
            peripheral_uuid = peripheral_config_dict["uuid"]
            peripheral_setup_dict = self.get_peripheral_setup_dict(peripheral_uuid)

            # Verify valid peripheral config dict
            if peripheral_setup_dict == None:
                self.logger.critical("Invalid peripheral uuid in device "
                    "config. Validator should have caught this.")
                continue

            # Get peripheral module and class name
            module_name = "device.peripherals.drivers." + peripheral_setup_dict["module_name"]
            class_name = peripheral_setup_dict["class_name"]

            # Import peripheral library
            module_instance= __import__(module_name, fromlist=[class_name])
            class_instance = getattr(module_instance, class_name)

            # Create peripheral manager
            peripheral_name = peripheral_config_dict["name"]
            if os.environ.get('SIMULATE') == "true":
                simulate = True
            else:
                simulate = False
            peripheral_manager = class_instance(peripheral_name, self.state, peripheral_config_dict, simulate)
            self.peripheral_managers[peripheral_name] = peripheral_manager


    def get_peripheral_setup_dict(self, uuid):
        """ Gets peripheral setup dict for uuid in peripheral setup table. """
        if not PeripheralSetupModel.objects.filter(uuid=uuid).exists():
            return None
        return json.loads(PeripheralSetupModel.objects.get(uuid=uuid).json)


    def spawn_peripheral_threads(self):
        """ Spawns peripheral threads. """
        if self.peripheral_managers == None:
            self.logger.info("No peripheral threads to spawn")
        else:
            self.logger.info("Spawning peripheral threads")
            for peripheral_name in self.peripheral_managers:
                self.peripheral_managers[peripheral_name].spawn()


    def create_controller_managers(self):
        """ Creates controller managers. """
        self.logger.info("Creating controller managers")

        # Verify controllers are configured
        if self.config_dict["controllers"] == None:
            self.logger.info("No controllers configured")
            return

        # Create controller managers
        self.controller_managers = {}
        for controller_config_dict in self.config_dict["controllers"]:
            self.logger.debug("Creating {}".format(controller_config_dict["name"]))
            
            # Get controller setup dict
            controller_uuid = controller_config_dict["uuid"]
            controller_setup_dict = self.get_controller_setup_dict(controller_uuid)

            # Verify valid controller config dict
            if controller_setup_dict == None:
                self.logger.critical("Invalid controller uuid in device "
                    "config. Validator should have caught this.")
                continue

            # Get controller module and class name
            module_name = "device.controllers.drivers." + controller_setup_dict["module_name"]
            class_name = controller_setup_dict["class_name"]

            # Import controller library
            module_instance= __import__(module_name, fromlist=[class_name])
            class_instance = getattr(module_instance, class_name)

            # Create controller manager
            controller_name = controller_config_dict["name"]
            controller_manager = class_instance(controller_name, self.state, controller_config_dict)
            self.controller_managers[controller_name] = controller_manager


    def spawn_controller_threads(self):
        """ Spawns controller threads. """
        if self.controller_managers == None:
            self.logger.info("No controller threads to spawn")
        else:
            self.logger.info("Spawning controller threads")
            for controller_name in self.controller_managers:
                self.controller_managers[controller_name].spawn()


    def all_threads_initialized(self):
        """ Checks that all recipe, peripheral, and controller 
            theads are initialized. """
        if self.state.recipe["mode"] == Modes.INIT:
            return False
        elif not self.all_peripherals_initialized():
            return False
        elif not self.all_controllers_initialized():
            return False
        return True


    def all_peripherals_initialized(self):
        """ Checks that all peripheral threads have transitioned from INIT. """
        for peripheral_name in self.state.peripherals:
            peripheral_state = self.state.peripherals[peripheral_name]

            # Check if mode in peripheral state
            if "mode" not in peripheral_state:
                return False

            # Check if mode either init or setup
            if peripheral_state["mode"] == Modes.INIT or peripheral_state["mode"] == Modes.SETUP:
                return False
        return True


    def all_controllers_initialized(self):
        """ Checks that all controller threads have transitioned from INIT. """
        for controller_name in self.state.controllers:
            controller_state = self.state.controllers[controller_name]
            if controller_state["mode"] == Modes.INIT:
                return False
        return True


    def kill_peripheral_threads(self):
        """ Kills all peripheral threads. """
        for peripheral_name in self.peripherals:
            self.peripherals[peripheral_name].thread_is_active = False


    def kill_controller_threads(self):
        """ Kills all controller threads. """
        for controller_name in self.controller:
            self.controller[controller_name].thread_is_active = False


    def shutdown_peripheral_threads(self):
        """ Shuts down peripheral threads. """
        for peripheral_name in self.peripherals:
            self.periphrals[peripheral_name].commanded_mode = Modes.SHUTDOWN


    def shutdown_controller_threads(self):
        """ Shuts down controller threads. """
        for controller_name in self.controllers:
            self.controllers[controller_name].commanded_mode = Modes.SHUTDOWN


################################# Events ######################################


    def process_event(self, request):
        """ Processes an event. Gets request parameters, executes request, returns 
            response. """

        # Get request parameters
        try:
            request_type = request["type"]
            request_value = request["value"]
        except KeyError as e:
            self.logger.exception("Invalid request parameters")
            self.response = {"status": 400, "message": "Invalid request parameters: {}".format(e)}
            return

        # Execute request
        if request_type== "Load Recipe":
            self.process_load_recipe_event()
        elif request_type == "Start Recipe":
            self.process_start_recipe_event(request_value)
        elif request_type == "Stop Recipe":
            self.process_stop_recipe_event()
        elif request_type == "Reset":
            self.process_reset_event()
        elif request_type == "Configure":
            self.process_configure_event()
        else:
            self.logger.info("Received invalid event request type: {}".format(request_type))


    def process_load_recipe_event(self):
        """ Processes load recipe event. """
        self.logger.critical("Loading recipe")
        self.response = {"status": 200, "message": "Pretended to load recipe"}


    def process_start_recipe_event(self, request_value):
        """ Processes load recipe event. """
        self.logger.debug("Processing start recipe event")


        # TODO: Check for valid mode transition


        # Send start recipe command to recipe thread
        self.recipe.commanded_recipe_uuid = request_value
        self.recipe.commanded_mode = Modes.START

        # Wait for recipe to be picked up by recipe thread or timeout event
        start_time_seconds = time.time()
        timeout_seconds = 10
        while True:
            # Exit when recipe thread picks up new recipe
            if self.recipe.commanded_mode == None:
                self.response = {"status": 200, "message": "Started recipe: {}".format(request_value)}
                break

            # Exit on timeout
            if time.time() - start_time_seconds > timeout_seconds:
                self.logger.critical("Unable to start recipe within 10 seconds. Something is wrong with code.")
                self.response = {"status": 500, "message": "Unable to start recipe, thread did not change state withing 10 seconds. Something is wrong with code."}
                break


    def process_stop_recipe_event(self):
        """ Processes load recipe event. """
        self.logger.debug("Processing stop recipe event")

        # TODO: Check for valid mode transition

        # Send stop recipe command
        self.recipe.commanded_mode = Modes.STOP

        # Wait for recipe to be picked up by recipe thread or timeout event
        start_time_seconds = time.time()
        timeout_seconds = 10
        while True:
            # Exit when recipe thread transitions to NORECIPE
            if self.recipe.mode == Modes.NORECIPE:
                self.response = {"status": 200, "message": "Stopped recipe"}
                break

            # Exit on timeout
            if time.time() - start_time_seconds > timeout_seconds:
                self.logger.critical("Unable to stop recipe within 10 seconds. Something is wrong with code.")
                self.response = {"status": 500, "message": "Unable to stop recipe, thread did not change state withing 10 seconds. Something is wrong with code."}
                break


    def process_reset_event(self):
        """ Processes reset event. """
        self.logger.debug("Processing reset event")
        self.response = {"status": 200, "message": "Pretended to reset device"}


    def process_configure_event(self):
        """ Processes configure event. """
        self.logger.debug("Processing configure event")
        self.response = {"status": 200, "message": "Pretended to configure device"}