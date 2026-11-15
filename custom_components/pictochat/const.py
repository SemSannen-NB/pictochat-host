"""Constants of the PictoChat integration."""

DOMAIN = "pictochat"

CONF_TOKEN = "token"
DEFAULT_PORT = 7777

# options
OPT_PHRASES = "phrases"
OPT_THRESHOLD = "threshold"            # percent; a message matches a phrase at or above this score
OPT_UNKNOWN_REPLY = "unknown_reply"    # text sent back to the DS when a message matches nothing ("" = none)
DEFAULT_THRESHOLD = 97

ACTIONS = ["toggle", "turn_on", "turn_off"]
TARGET_DOMAINS = ["light", "switch", "input_boolean", "fan", "script", "scene", "button", "input_button"]

# events on the Home Assistant bus
EVENT_PHRASE = "pictochat_phrase"      # a message matched a phrase
EVENT_MESSAGE = "pictochat_message"    # every picture from a DS, matched or not

PLATFORMS = ["binary_sensor", "event", "image", "notify", "sensor"]
