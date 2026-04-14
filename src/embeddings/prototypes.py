"""
Representative prototype texts for topic and event-type domains.

These serve as query vectors for cosine similarity matching.
Designed to match the vocabulary of a hardware engineering team's Slack.
"""

# Representative description of each topic domain
TOPIC_PROTOTYPES: dict[str, str] = {
    "connector": (
        "connector pin socket header harness MX150 Molex Amphenol TE Connectivity "
        "footprint battery connector current rating pin pitch height mating"
    ),
    "pcb": (
        "PCB board layout schematic rev copper pour DRC trace via pad gerber spin "
        "thermal pad pull-up resistor capacitance rev C rev B"
    ),
    "firmware": (
        "firmware I2C SPI UART bus driver register bootloader flash initialize "
        "sequence timing bring-up clock SCL SDA stuck low timeout delay"
    ),
    "bms": (
        "BMS battery PMIC fuel gauge power management MAX17261 charging cell "
        "power good POWER_GOOD battery harness"
    ),
    "thermal": (
        "thermal temperature cycling soak heat dissipation pad underfill cross-section "
        "failure rate celsius 85C 40C voltage regulator U7 qualification"
    ),
    "testing": (
        "test validation qualification fail failure spec measurement result cycle "
        "pass iteration bring-up suite automated test run"
    ),
    "supply_chain": (
        "supplier lead time availability stock distributor allocation procurement "
        "BOM Digi-Key part number Winbond Molex TE shortage"
    ),
    "sensor": (
        "sensor SHT40 IMU ADC humidity reading sample intermittent environmental "
        "measurement bus noise I2C interference"
    ),
    "nor_flash": (
        "NOR flash Winbond Macronix W25Q128 MX25L storage memory allocation "
        "write protection status register"
    ),
}

# Representative description of each event type
EVENT_TYPE_PROTOTYPES: dict[str, str] = {
    "blocker": (
        "blocked blocking reproducible hang stuck fail failure cannot proceed prevents "
        "stops crash 100 percent all units fail validation blocked"
    ),
    "decision": (
        "decision needed alignment vote we should recommendation agreed choose option "
        "trade-off tentative decision which direction should we"
    ),
    "risk": (
        "risk at risk lead time delay slip qualification failure rate out of spec "
        "shortage P1 severity schedule risk program risk"
    ),
    "status_update": (
        "update status FYI heads up preliminary results are in for awareness logging "
        "completed in progress landed confirmed"
    ),
    "request_for_input": (
        "can you could you do you have please confirm flagging looping input thoughts "
        "any view have you seen requesting feedback from"
    ),
    "noise": (
        "lunch snacks brownies laughing coffee birthday celebration happy hour social "
        "team lunch off topic weekend plans"
    ),
}
