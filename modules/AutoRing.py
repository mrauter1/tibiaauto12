from conf.Constants import Rings
from engine.ScanRing import ScanRing
from modules.support_ui import EquipmentSupport

EnabledAutoRing = False


class AutoRing(EquipmentSupport):
    def __init__(self, root, RingPositions, HealthLocation, MOUSE_OPTION, ItemsPath):
        del root, ItemsPath
        self._equipment_init(
            key="AutoRing",
            title="Module: Auto Ring",
            loader="RingLoader",
            item_label="Ring",
            items=Rings,
            positions=RingPositions,
            life_location=HealthLocation,
            mouse_option=MOUSE_OPTION,
            empty_sensor=ScanRing,
            worker_name="ThreadAutoRing",
        )
