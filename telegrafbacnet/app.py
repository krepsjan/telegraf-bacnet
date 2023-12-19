import logging
from os import getpid
from typing import Any

from bacpypes.apdu import (
    ConfirmedCOVNotificationRequest,
    IAmRequest,
    ReadPropertyACK,
    ReadPropertyMultipleACK,
    ReadPropertyRequest,
)
from bacpypes.app import BIPSimpleApplication
from bacpypes.constructeddata import Array, ArrayOf
from bacpypes.core import deferred
from bacpypes.iocb import IOCB
from bacpypes.local.device import LocalDeviceObject
from bacpypes.object import get_datatype, get_object_class
from bacpypes.pdu import Address
from bacpypes.primitivedata import ObjectIdentifier, Unsigned

from .config import Config, DeviceConfig, DiscoveryGroupConfig, ObjectConfig
from .influx import InfluxLPR
from .tasks import (
    DeviceReadTask,
    DiscoveryTask,
    ObjectReadTask,
    SubscribeCOVTask,
)


_logger = logging.getLogger(__name__)


class TelegrafApplication(BIPSimpleApplication):
    """Main BACnet application class"""

    def __init__(self, config: Config):
        # _logger.info("======================")
        # _logger.info("initialization")
        # _logger.info(config.device_name)
        # _logger.info(config)
        
        local_device = LocalDeviceObject(
            objectName=config.device_name,
            objectIdentifier=config.device_identifier,
            maxApduLengthAccepted=config.max_apdu_length_accepted,
            segmentationSupported=config.segmentation_supported,
            vendorIdentifier=config.vendor_identifier,
        )
        super().__init__(local_device, config.address)
        self.config = config
        self.devices: dict[Address, DeviceConfig] = {}
        self.influx_lpr = InfluxLPR()
        if self.config.discovery.enabled:
            DiscoveryTask(self, self.config.discovery).install_task()
        self.tags_mapping = {}

            
    def _print_measurement(self, address: Address,
                           object_identifier: tuple[str, int],
                           prop: str, value: Any,
                           index: int | None = None) -> None:
        
        if address not in self.devices:
            _logger.warning("Skipping measurement from unknown device %r",
                            address)
            return
        device = self.devices[address]
        # _logger.info(" === device info in print_measurement === ")
        # _logger.info(device)
        # _logger.info(object_identifier)
        if device.device_name is None and device.device_identifier is None:
            _logger.error("%r has neither identifier or name, skipping",
                          device)
            return
        sensorType = self.tags_mapping.get((address.dict_contents(),object_identifier[0], object_identifier[1]), 'Unidentified')
        tags: list[tuple[str, str | int | float]] = [
            ("deviceAddress", str(address)),
            ("objectType", object_identifier[0]),
            ("objectInstanceNumber", object_identifier[1]),
            ("sensorType", sensorType),
        ]
        if device.device_identifier is not None:
            tags.append(("deviceIdentifier", device.device_identifier))
        if device.device_name is not None:
            tags.append(("deviceName", device.device_name))
        if index is not None:
            tags.append(("propertyArrayIndex", index))
        self.influx_lpr.print(prop, value, *tags)

    # Measurements reading

    def _process_read_property_ack(self, apdu: ReadPropertyACK) -> None:
        datatype = get_datatype(apdu.objectIdentifier[0],
                                apdu.propertyIdentifier)
        if not datatype:
            _logger.error("unknown datatype in a response from %r",
                          apdu.pduSource)
            return

        if issubclass(datatype, Array) and apdu.propertyArrayIndex is not None:
            if apdu.propertyArrayIndex == 0:
                value = apdu.propertyValue.cast_out(Unsigned)
            else:
                value = apdu.propertyValue.cast_out(datatype.subtype)
        else:
            value = apdu.propertyValue.cast_out(datatype)
        # _logger.info("=============================================")
        # _logger.info("pduSource %r", apdu.pduSource)
        # _logger.info("ObjectIdentifier %r", apdu.objectIdentifier)
        # _logger.info("ProperyIdentifier %r", apdu.properyidentifier)
        # _logger.info("Value %r" , value)
        self._print_measurement(apdu.pduSource, apdu.objectIdentifier,
                                apdu.propertyIdentifier, value)

    def _process_read_property_multiple_ack(self,
                                            apdu: ReadPropertyMultipleACK) \
            -> None:
        # pokud jsou definovane napr 3 objectinstance, ktere na device chci cist
        # budou v tomhle poli 3 prvky. Klic je objectIdentifier = ('analogValue', 55)
        for result in apdu.listOfReadAccessResults:
            #_logger.info("==> objectIdentifier %r", result.objectIdentifier) # to funguje, vraci to ten objectIdentifier
            # tady je vetsinou v mem pripade jen jedna value a to je propertyIdentifier = 'presentValue'
            # ale mozna ze pro multivalued hodnotu by tam mohlo byt tech prvku vic?
            for element in result.listOfResults:
                if element.readResult.propertyAccessError is not None:
                    _logger.error("Error while ReadingPropertyMultiple %r",
                                  element.readResult.propertyAccessError)
                    continue
                                      # (objType, property)
                                      # vraci datatype class.                       
                datatype = get_datatype(result.objectIdentifier[0],  # tohle nevim, muselo by se zjistit z dokumentace, ale podle vsecho kontroluje, zda vraceny typ odpovida tomu ocekavanemu. 
                                        element.propertyIdentifier) # element.propertyIdentifier je v mem pripade presentValue
                if not datatype:
                    _logger.error("unknown datatype in a response from %r",
                                  apdu.pduSource)
                    continue

                # je to pole
                #
                # tady se z apdu ve tvaru
                #               <bacpypes.primitivedata.Tag(real) instance at 0x7f8a8cfea380>
                                    # tagClass = 0 application
                                    # tagNumber = 4 real
                                    # tagLVT = 4
                                    # tagData = '41.aa.66.66'
                # zkonstruuje ta hodnota pro zapis do influx. Napr. 24,58 stupne.                     
                if issubclass(datatype, Array) \
                        and element.propertyArrayIndex is not None:
                    if element.propertyArrayIndex == 0:
                        value = element.readResult.propertyValue.cast_out(
                            Unsigned)
                    else:
                        value = element.readResult.propertyValue.cast_out(
                            datatype.subtype,
                        )
                # neni to pole (vetsinou v mem pripade)        
                else:
                    value = element.readResult.propertyValue.cast_out(datatype)

                # Tohle uz jsou vracene responses z bacnet device. V tech jsou jen informace o tom,
                # jaky je to objekt a property a jaka je hodnota.
                # Takze napr. info o tom, ze pduSource <Address 10.32.7.25>
                # a ('multiStateValue', 23), nebo ('analogValue', 54)
                # Nicmene, ta device mi neodpovi, ze to odpovida tomu tagu, co jsem mel
                # v konfiguraku.
                # To je naprd. Jak tam to info dostat? Rozsirit nejak tu response na zaklade toho
                # co jsem mel v request? Jde to nejak? Jinak jsem asi v hajzlu a musim se vratit k tomu, ze                
                # budu cist multiple veci a muset mit konfiguraci na vice mistech.

                # Slo by to tak, ze na zaklade kombinace hodnot v requestu, tedy napr. z konfigu mam
                # ze se ma precist z device 10.32.7.30 multipleProperty a k tomu
                # vzdy tuple napr. analogValue, 55
                # tak kombinaci tech tri hodnot mohu priradit urcity tag. Tabulku tech tagu
                # bych musel mit globalni a vytvorit ji v okamziku, kdy nacitam konfiguraci.
                # Pak vzdycky pred printem hodnoty si nacist podle te kombinace z tabulky odpovidajici tag
                # a ten pridat do hodnoty zapisovane do influxdb.
                
                # To znamena, vic si precist o tech response/requests
                # _logger.info("================ apdu.pduSource ========================")
                # _logger.info("pduSource %r", apdu.pduSource)
                # _logger.info("pduSource.dict_contents() %r", apdu.pduSource.dict_contents())
                # _logger.info("pduSource.dict_contents() type %r", type(apdu.pduSource.dict_contents()))                                
                # _logger.info("pduSource type %r", type(apdu.pduSource))
                # _logger.info("ObjectIdentifier %r", result.objectIdentifier)
                # _logger.info("ObjectIdentifier type %r", type(result.objectIdentifier))                
                # _logger.info(apdu.debug_contents())
                #_logger.info("ProperyIdentifier %r", element.properyIdentifier)
                #_logger.info("ProperyArrayIndex %r", element.properyArrayIndex)
                # propertyIdentifier v nekterych pripadech neni definovan, ale to nevadi, pro zkonstruovani tag table mi staci
                # pduSource a objectIdentifier
                #2023-12-18 17:16:48,120 [INFO] telegrafbacnet.app: ================ apdu.pduSource ========================
                #2023-12-18 17:16:48,120 [INFO] telegrafbacnet.app: pduSource <Address 10.32.7.25>      
                #2023-12-18 17:16:48,121 [INFO] telegrafbacnet.app: ObjectIdentifier ('multiStateValue', 23)

                # print_measurement jsou ty hodnoty dostupny taky, takze by se tag mohl vycitat az tam.
                # konstrukci provest tam, kde se dela register_devices, viz:
                #
                # 2023-12-18 17:23:29,168 [INFO] telegrafbacnet.app: ==== register_devices =============
                # 2023-12-18 17:23:29,169 [INFO] telegrafbacnet.app: <class 'telegrafbacnet.config.DeviceConfig'>
                # 2023-12-18 17:23:29,169 [INFO] telegrafbacnet.app: <Object ObjectIdentifier(multiStateValue,23)>
                # 2023-12-18 17:23:29,170 [INFO] telegrafbacnet.app: Temperature
                # 2023-12-18 17:23:29,170 [INFO] telegrafbacnet.app: <class 'telegrafbacnet.config.DeviceConfig'>
                # 2023-12-18 17:23:29,170 [INFO] telegrafbacnet.app: <Object ObjectIdentifier(analogValue,55)>
                # 2023-12-18 17:23:29,171 [INFO] telegrafbacnet.app: Temperature
                # 2023-12-18 17:23:29,171 [INFO] telegrafbacnet.app: <Object ObjectIdentifier(binaryValue,6)>
                # 2023-12-18 17:23:29,171 [INFO] telegrafbacnet.app: PresenceDetected
                # 2023-12-18 17:23:29,171 [INFO] telegrafbacnet.app: <Object ObjectIdentifier(analogValue,54)>
                # 2023-12-18 17:23:29,172 [INFO] telegrafbacnet.app: AirQuality


#                _logger.info("Value %r" , value)
                    
                self._print_measurement(apdu.pduSource,
                                        result.objectIdentifier,
                                        element.propertyIdentifier, value,
                                        element.propertyArrayIndex)

    # TAHLE metoda se registruje jako DeviceReadTask pro device in devices  
    # Pak vola ruzne interni metody podle typu ioResponse, ktera zas obratem
    # zavisi na tom, jaka byla konfigurace dane device - tj. zda ma nakonfigurovany
    # multiple pristup, nebo ne.
    # Protoze ted mam VSECHNO multiple, tak bych mohl rozsirit jen metody pro
    # ReadPropertyMultipleACK, ale casem by to melo byt pro obe.
    # Jako parametr se predava pouze apdu, tedy Application Protocol Data Unit. 
    def _process_response_iocb(self, iocb: IOCB, **_: Any) -> None:
        if iocb.ioError:
            _logger.error("Response IOCB error: %r", iocb.ioError)
            return
        if not iocb.ioResponse:
            _logger.error("No error nor response in IOCB response")
            return

        apdu = iocb.ioResponse
        _logger.debug("Received %r from %r", type(apdu), apdu.pduSource)
        if isinstance(apdu, ReadPropertyACK):
            self._process_read_property_ack(apdu)
        elif isinstance(apdu, ReadPropertyMultipleACK):
            self._process_read_property_multiple_ack(apdu)
        else:
            _logger.debug("Unhandled response type %r", type(apdu))

    def do_UnconfirmedCOVNotificationRequest(
            self, apdu: ConfirmedCOVNotificationRequest,
    ) -> None:
        if apdu.subscriberProcessIdentifier != getpid():
            _logger.debug("Ignoring COV notification not intended to me")
            return
        _logger.debug("Received COV notification from %r", apdu.pduSource)

        for element in apdu.listOfValues:
            element_value = element.value.tagList
            if len(element_value) == 1:
                element_value = element_value[0].app_to_object().value

            # _logger.info("=============================================")
            # _logger.info("pduSource %r", apdu.pduSource)
            # _logger.info("ObjectIdentifier %r", apdu.objectIdentifier)
            # _logger.info("ProperyIdentifier %r", apdu.properyidentifier)
            # _logger.info("Value %r" , element_value)

            self._print_measurement(apdu.pduSource,
                                    apdu.monitoredObjectIdentifier,
                                    element.propertyIdentifier, element_value)

    # Device discovery

    def _process_read_object_list_response(
        self, iocb: IOCB, device: DeviceConfig,
        discovery_group: DiscoveryGroupConfig,
    ) -> None:
        if iocb.ioError:
            _logger.error("Error reading object list of %r: %r", device,
                          iocb.ioError)
            return
        if not iocb.ioResponse:
            _logger.error("No error nor response in IOCB response")
            return

        apdu = iocb.ioResponse
        _logger.debug("Received %r from %r", type(apdu), apdu.pduSource)
        if not isinstance(apdu, ReadPropertyACK):
            _logger.error("APDU has invalid type %r", apdu)
            return
        if apdu.pduSource in self.devices:
            _logger.debug("Device @%r is already known, skipping",
                          apdu.pduSource)
            return
        object_list = apdu.propertyValue.cast_out(ArrayOf(ObjectIdentifier))
        objects: list[ObjectConfig] = []
        for object_identifier in object_list:
            if object_identifier[0] == "device":
                continue
            if discovery_group.object_types is not None \
                    and object_identifier[0] not in \
                    discovery_group.object_types:
                continue
            obj = ObjectConfig()
            obj.object_identifier = ObjectIdentifier(object_identifier)
            obj.read_interval = discovery_group.read_interval
            obj.cov = discovery_group.cov
            obj.cov_lifetime = discovery_group.cov_lifetime
            obj.properties = tuple(
                prop.identifier for prop
                in get_object_class(object_identifier[0]).properties
                if discovery_group.properties is None
                or str(prop.identifier) in discovery_group.properties
            )
            objects.append(obj)
        device.objects = tuple(objects)
        self.register_devices(device)

    def _process_read_device_name_response(self, iocb: IOCB,
                                           device: DeviceConfig) -> None:
        if iocb.ioError:
            _logger.error("Error reading name of %r: %r", device,
                          iocb.ioError)
            return
        if not iocb.ioResponse:
            _logger.error("No error nor response in IOCB response")
            return

        apdu: ReadPropertyACK = iocb.ioResponse
        datatype = get_datatype(apdu.objectIdentifier[0],
                                apdu.propertyIdentifier)
        if not datatype:
            _logger.error("unknown datatype in a response from %r",
                          apdu.pduSource)
            return

        device.device_name = apdu.propertyValue.cast_out(datatype)
        discovery_group = self.config.discovery.get_discovery_group(device)
        if discovery_group is None:
            _logger.debug("No discovery group for %r", device)
            return

        read_object_list_request = ReadPropertyRequest(
            destination=apdu.pduSource,
            objectIdentifier=ObjectIdentifier("device",
                                              device.device_identifier),
            propertyIdentifier="objectList",
        )
        iocb = IOCB(read_object_list_request)
        iocb.add_callback(self._process_read_object_list_response, device,
                          discovery_group)
        deferred(self.request_io, iocb, "_process_read_device_name_response")

    def do_IAmRequest(self, apdu: IAmRequest) -> None:
        if apdu.pduSource in self.devices:
            _logger.debug("Device @%r is already known, skipping",
                          apdu.pduSource)
            return
        device = DeviceConfig()
        device.address = apdu.pduSource
        device.device_identifier = apdu.iAmDeviceIdentifier[1]
        device.read_multiple = False
        read_object_list_request = ReadPropertyRequest(
            destination=apdu.pduSource,
            objectIdentifier=apdu.iAmDeviceIdentifier,
            propertyIdentifier="objectName",
        )
        iocb = IOCB(read_object_list_request)
        iocb.add_callback(self._process_read_device_name_response, device)
        deferred(self.request_io, iocb, "do_IAmRequest")

    def request_io(self, iocb: IOCB, source: str = "(unknown)") -> None:
        _logger.debug("Sending IOCB %r for %r", iocb.args, source)
        super().request_io(iocb)

    def register_devices(self, *devices: DeviceConfig) -> None:
        """
        Registers one or more devices in the application and installs required
        tasks
        """
        #_logger.info("==== register_devices =============")
        for device in devices:
            #_logger.info("device type: %r",type(device))
            # mam jednu device a na ni definovanych nekolik objektu, ktere chci cist. 
            for deviceObject in device.objects:
                # _logger.info("deviceObject in device %r", deviceObject) # to je <Object ObjectIdentifier(multiStateValue,23)>
                # _logger.info("deviceObject type in device %r", type(deviceObject))                
                # _logger.info("deviceObject sensor type %r", deviceObject.sensorType)  # typ
                # _logger.info("deviceObject properties %r", deviceObject.properties)   # to, co chci cist, tedy presentValue
                # _logger.info("deviceObject identifier %r", deviceObject.object_identifier.value[0])
                # _logger.info("deviceObject identifier %r", deviceObject.object_identifier.value[1])                
                # _logger.info("device ADDRESS %r", device.address.dict_contents())                
                # register tags for all deviceObjects.
                self.tags_mapping[(device.address.dict_contents(), deviceObject.object_identifier.value[0], deviceObject.object_identifier.value[1])] = deviceObject.sensorType
        #_logger.info("tags_mapping values %r", self.tags_mapping)
        for device in devices:
            if device.read_multiple \
                    and any(not object.cov for object in device.objects):
                DeviceReadTask(self, device, self.config,
                               self._process_response_iocb).install_task()
            for obj in device.objects:
                if obj.cov:
                    SubscribeCOVTask(self, obj,
                                     device, self.config).install_task()
                elif not device.read_multiple:
                    ObjectReadTask(self, obj, device, self.config,
                                   self._process_response_iocb).install_task()
            self.devices[device.address] = device
