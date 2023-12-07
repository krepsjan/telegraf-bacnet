from multiprocessing import Process, Queue
from time import time_ns
from typing import Any


class InfluxLine:
    """Class representing a single InfluxDB measurement"""

    def __init__(self, key: str, value: Any, *tags: tuple[str, Any]) -> None:
        self.key = key
        self.value = value
        self.tags = tags
        self.timestamp = time_ns()


class InfluxLPR:
    """Class for printing measurements in InfluxDB Line Protocol format"""

    def __init__(self) -> None:
        self.queue: Queue[InfluxLine] = Queue()
        self.print_job = Process(target=self._print_task)
        self.print_job.start()

    def print(self, key: str, value: Any, *tags: tuple[str, Any]) -> None:
        """Adds the measurement to the print queue"""
        self.queue.put(InfluxLine(key, value, *tags))

    def _print_task(self) -> None:
        try:
            while True:
                # bere hodnoty z fronty a formatuje je do InfluxDB Line Protocol
                line = self.queue.get(block=True)
                self._print_influx_line(line)
        except KeyboardInterrupt:
            pass

    @staticmethod
    def _print_influx_line(line: InfluxLine) -> None:
        tags_str = ",".join(f"{tagKey}={tagValue}"
                            for tagKey, tagValue in line.tags)
        tags_str = f",{tags_str}" if tags_str else tags_str
        value = line.value
        if isinstance(value, list):
            for index, inner in enumerate(value):
                print(f"bacnet{tags_str},index={index} "
                      f"{line.key}={inner} {line.timestamp}")
        # key je to, na co se ptam, value je hodnota, ktera se vrati
        # Bylo by to potreba zapracovat do toho, aby se to posilalo do influxu
        #                     
        elif line.value == "inactive":
            print(f"bacnet{tags_str} {line.key}=0 {line.timestamp}")
        elif line.value == "active":
            print(f"bacnet{tags_str} {line.key}=1 {line.timestamp}")
        else:
            # tady jsou ty tagy ,deviceAddress=10.32.7.27,objectType=analogValue,objectInstanceNumber=55,deviceName=E09_27
            # Ja tam chci pridat tag measuremetType=Temperature a pod. Tim bych se na to pak mohl ptat z Flask aplikace. A ty hodnoty by se rovnou 
            # pouzily jako klice v jsonu. Takze by se pri pridani merene veliciny pouze menila konfigurace pro telegfaf/bacnet a zbytek by zustal stejny.
            # za nimi je po mezerach key=value, v pripade ze je value binarni, je tam presentValue=inactive nebo presentValue=active
            # jenze, to neumi influxdb, takze to musim preformatovat na presentValue=0 nebo presentValue=1
            # nebo na presentValue=True nebo presentValue=False
            print(f"bacnet{tags_str} {line.key}={line.value} {line.timestamp}")
