from conf.Constants import Amulets
from engine.ScanAmulet import ScanAmulet
from modules.support_ui import EquipmentSupport

EnabledAutoSSA = False


class AutoSSA(EquipmentSupport):
    def __init__(self, root, AmuletPositions, HealthLocation, MOUSE_OPTION, ItemsPath):
        del root, ItemsPath
        self._equipment_init(
            key="AutoSSA",
            title="Module: Auto Amulet",
            loader="AmuletLoader",
            item_label="Amulet",
            items=Amulets,
            positions=AmuletPositions,
            life_location=HealthLocation,
            mouse_option=MOUSE_OPTION,
            empty_sensor=ScanAmulet,
            worker_name="ThreadAutoSSA",
        )
