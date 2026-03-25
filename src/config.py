# Pin assignments — adjust for your wiring
STEP_PIN = 2
DIR_PIN = 3
ENABLE_PIN = 4
HOMING_PIN = 5

# Homing sensor: NC (normally-closed) limit switch
# activeState=0 means the pin reads 0 when the sensor is triggered
HOMING_ACTIVE_STATE = 0
HOMING_DIRECTION = "down"  # direction of travel toward the home sensor
# Position (mm) that the home sensor edge represents in the motion coordinate
# space. 0.0 means the sensor is at position 0 (MIN_MM is then the distance
# from sensor to the start of the usable range). Negative values place the
# sensor before the motion origin; positive values place it inside the range.
HOME_SENSOR_MM = 0.0

# Mechanical configuration
PULLEY_TEETH = 20
BELT_PITCH_MM = 2.0
STEPS_PER_REV = 200
MICROSTEPS = 8  # set to match your motor driver's microstepping configuration

# Derived: steps per mm = steps_per_rev * microsteps / (teeth * pitch)
STEPS_PER_MM = STEPS_PER_REV * MICROSTEPS / (PULLEY_TEETH * BELT_PITCH_MM)  # 40.0

# Motion limits (mm)
MIN_MM = 10.0
MAX_MM = 160.0  # adjust to match your physical build

# Speed / acceleration limits
MAX_SPEED_MM_S = 600.0
MAX_ACCEL_MM_S2 = 3000.0
MIN_SPEED_MM_S = 1.0

# BLE
DEVICE_NAME = "OSSM"

# BLE UUIDs — same as ossm/ reference firmware for remote compatibility
SERVICE_UUID = "522b443a-4f53-534d-0001-420badbabe69"
PRIMARY_COMMAND_UUID = "522b443a-4f53-534d-1000-420badbabe69"
SPEED_KNOB_UUID = "522b443a-4f53-534d-1010-420badbabe69"
CURRENT_STATE_UUID = "522b443a-4f53-534d-2000-420badbabe69"
PATTERN_LIST_UUID = "522b443a-4f53-534d-3000-420badbabe69"
PATTERN_DESCRIPTION_UUID = "522b443a-4f53-534d-3010-420badbabe69"
