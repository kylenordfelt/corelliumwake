import configparser
import logging
import gpiozero
import sys

# --- Setup logging ---
log_file = '/tmp/fakewake_test.log'
logging.basicConfig(level=logging.DEBUG,
                    filename=log_file,
                    filemode='w',
                    format='%(asctime)s:%(levelname)s:%(message)s')

console = logging.StreamHandler(sys.stdout)
console.setLevel(logging.DEBUG)
formatter = logging.Formatter('%(levelname)s:%(message)s')
console.setFormatter(formatter)
logging.getLogger('').addHandler(console)

logging.info("Starting config + GPIO test...")

# --- Default config fallback (as used in your main script) ---
default_config = {
    'power': '23',
    'reset': '24',
    'psu_sense': '25',
    'psu_sense_active_low': 'False',
    'aux1': '17',
    'aux2': '27',
}

# --- Parse config ---
config = configparser.ConfigParser()
config.read_dict({'pins': default_config})
config.read('config.ini')

try:
    POWER_PIN = config.getint('pins', 'power')
    RESET_PIN = config.getint('pins', 'reset')
    PSU_SENSE_PIN = config.getint('pins', 'psu_sense')
    PSU_SENSE_ACTIVE_LOW = config.getboolean('pins', 'psu_sense_active_low')
    ##AUX1_PIN = config.getint('pins', 'aux1')
    ##AUX2_PIN = config.getint('pins', 'aux2')

    logging.debug(f"POWER_PIN = {POWER_PIN}")
    logging.debug(f"RESET_PIN = {RESET_PIN}")
    logging.debug(f"PSU_SENSE_PIN = {PSU_SENSE_PIN}")
    ##logging.debug(f"AUX1_PIN = {AUX1_PIN}")
    ##logging.debug(f"AUX2_PIN = {AUX2_PIN}")
    logging.debug(f"PSU_SENSE_ACTIVE_LOW = {PSU_SENSE_ACTIVE_LOW}")

    # --- Create GPIO objects ---
    logging.info("Creating GPIO devices...")
    POWER_SWITCH = gpiozero.DigitalOutputDevice(POWER_PIN)
    RESET_SWITCH = gpiozero.DigitalOutputDevice(RESET_PIN)
    PSU_SENSE = gpiozero.DigitalInputDevice(PSU_SENSE_PIN, pull_up=PSU_SENSE_ACTIVE_LOW)
    ##AUX1 = gpiozero.DigitalOutputDevice(AUX1_PIN)
    ##AUX2 = gpiozero.DigitalOutputDevice(AUX2_PIN)
    
    logging.info("GPIO setup successful.")

except Exception as e:
    logging.error(f"Exception occurred: {e}", exc_info=True)
    sys.exit(1)
