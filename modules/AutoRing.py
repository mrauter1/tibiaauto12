from conf.Constants import Rings
from tibia_client.models import SensorKind
from modules.support_ui import EquipmentSupport


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
            empty_sensor=SensorKind.RING_EMPTY,
            worker_name="ThreadAutoRing",
        )
