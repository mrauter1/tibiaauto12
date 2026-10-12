from conf.Constants import Amulets
from tibia_client.models import SensorKind
from modules.support_ui import EquipmentSupport


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
            empty_sensor=SensorKind.AMULET_EMPTY,
            worker_name="ThreadAutoSSA",
        )
