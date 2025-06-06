"""Solarfocus abstract component"""
import logging

from ...modbus_wrapper import ModbusConnector
from .data_value import DataValue
from .enums import DataTypes, RegisterTypes
from .performance_calculator import PerformanceCalculator
from .register_slice import RegisterSlice


class Component:
    def __init__(self, input_address: int, holding_address: int = -1) -> None:
        self.input_address = input_address
        self.input_count = 0
        self.holding_address = holding_address
        self.holding_count = 0
        self.__data_values = None
        self.__input_values = None
        self.__holding_values = None
        self.__performance_calculators = None
        self.__modbus = None

    def initialize(self, modbus: ModbusConnector):
        """
        Initializes the absolute addresses of the DataValues and the count of the Component
        """

        self.__modbus = modbus

        for _, value in self.__get_data_values():

            if value.register_type == RegisterTypes.INPUT:
                value.absolut_address = self.input_address
            else:
                value.absolut_address = self.holding_address
                # Holding registers can write to the heating system
                value.modbus = modbus

        # Dynamically calculate how many registers have to be read
        if len(self.__get_input_values()) > 0:
            _, last_input_value = self.__get_input_values()[-1]
            self.input_count = last_input_value.address + last_input_value.count

        if len(self.__get_holding_values()) > 0:
            _, last_holding_value = self.__get_holding_values()[-1]
            self.holding_count = last_holding_value.address + last_holding_value.count

        # Calculate the address slices we need to read
        # This is necessary because the modbus protocol can block the read/write of some registers
        # => if one of these registers is between the start and end address of the read/write we need to skip it
        self.__input_slices = Component._calculate_ranges([v for (n, v) in self.__get_input_values()])
        self.__holding_slices = Component._calculate_ranges([v for (n, v) in self.__get_holding_values()])

        # Initialize has_performance_calculators
        self.__get_performance_calculators()

        return self

    @property
    def holding_slices(self) -> list[RegisterSlice]:
        """
        Returns the address slices of the holding registers
        """
        return self.__holding_slices

    @property
    def input_slices(self) -> list[RegisterSlice]:
        """
        Returns the address slices of the input registers
        """
        return self.__input_slices

    @staticmethod
    def _calculate_ranges(datavalues: list[DataValue]) -> list[RegisterSlice]:
        slices = []
        if len(datavalues) < 1:
            return slices
        current_slice = RegisterSlice(datavalues[0].get_absolute_address(), datavalues[0].address, datavalues[-1].address + datavalues[-1].count)
        for i, datavalue in enumerate(datavalues[:-1]):
            if datavalue.address + datavalue.count != datavalues[i + 1].address:
                # A gap was found => define the range from the start of the current slice to the end of the current address+count
                current_slice.count = (datavalue.address + datavalue.count) - current_slice.relative_address
                slices.append(current_slice)
                # the new count is the distance between the next address and the last address
                new_count = (datavalues[-1].address + datavalues[-1].count) - datavalues[i + 1].address
                current_slice = RegisterSlice(datavalues[i + 1].get_absolute_address(), datavalues[i + 1].address, new_count)
        slices.append(current_slice)
        return slices

    @property
    def has_input_address(self) -> bool:
        return self.input_address >= 0 and self.input_count > 0

    @property
    def has_holding_address(self) -> bool:
        return self.holding_address >= 0 and self.holding_count > 0

    @property
    def has_performance_calculators(self) -> bool:
        return len(self.__get_performance_calculators()) > 0

    def __get_data_values(self) -> list[tuple[str, DataValue]]:
        """
        Returns all DataValues of this Component
        """
        if self.__data_values is None:
            self.__data_values = [(k, v) for k, v in self.__dict__.items() if isinstance(v, DataValue)]
        return self.__data_values

    def __get_input_values(self) -> list[tuple[str, DataValue]]:
        """
        Get sorted list of all DataValues with RegisterType Input
        """
        if self.__input_values is None:
            self.__input_values = [(k, v) for k, v in self.__get_data_values() if v.register_type == RegisterTypes.INPUT]
            self.__input_values = sorted(self.__input_values, key=lambda item: item[1].address)
        return self.__input_values

    def __get_holding_values(self) -> list[tuple[str, DataValue]]:
        """
        Get sorted list of all DataValues with RegisterType Holding
        """
        if self.__holding_values is None:
            self.__holding_values = [(k, v) for k, v in self.__get_data_values() if v.register_type == RegisterTypes.HOLDING]
            self.__holding_values = sorted(self.__holding_values, key=lambda item: item[1].address)
        return self.__holding_values

    def __get_performance_calculators(self) -> list[tuple[str, PerformanceCalculator]]:
        """
        Returns all PerformanceCalculators of this Component
        """
        if self.__performance_calculators is None:
            self.__performance_calculators = [(k, v) for k, v in self.__dict__.items() if isinstance(v, PerformanceCalculator)]
        return self.__performance_calculators

    @staticmethod
    def __unsigned_to_signed(number: int, byte_count: int) -> int:
        return int.from_bytes(number.to_bytes(byte_count, "little", signed=False), "little", signed=True)

    def update(self) -> bool:
        """
        Retrieve current values from the heating system
        """
        failed = False
        if self.has_input_address:
            read_success, registers = self.__modbus.read_input_registers(self.input_slices, self.input_count)
            parsing_success = False
            if read_success:
                parsing_success = self._parse(registers, RegisterTypes.INPUT) and read_success
            failed = not parsing_success and read_success or failed
            if failed:
                logging.error(f"Failed to read input registers of {self.__class__.__name__}")

        if self.has_holding_address:
            read_success, registers = self.__modbus.read_holding_registers(self.holding_slices, self.holding_count)
            parsing_success = False
            if read_success:
                parsing_success = self._parse(registers, RegisterTypes.HOLDING) and read_success
            failed = not (parsing_success and read_success) or failed
            if failed:
                logging.error(f"Failed to read holding registers of {self.__class__.__name__}")
        return not failed

    def _parse(self, data: list[int], type: RegisterTypes) -> bool:
        """
        Dynamically assigns the values to the DataValues of this Component
        """
        if len(data) != (self.input_count if type == RegisterTypes.INPUT else self.holding_count):
            logging.error(
                f"Data length does not match the expected length of {self.input_count if type == RegisterTypes.INPUT else self.holding_count} for {self.__class__.__name__}"
            )
            return False

        encountered_error = False
        for name, value in self.__get_input_values() if type == RegisterTypes.INPUT else self.__get_holding_values():
            try:
                # Multi-register values (UINT32, INT32)
                if value.count == 2:
                    _value = (data[value.address] << 16) + data[value.address + 1]
                else:
                    _value = data[value.address]

                # Datatype
                if value.data_type == DataTypes.INT:
                    _value = Component.__unsigned_to_signed(_value, value.count * 2)

                # Store
                value.value = _value
            except:
                logging.exception(f"Error while parsing {name} of {self.__class__.__name__}")
                encountered_error = True
        return not encountered_error

    def __repr__(self) -> str:
        message = ["=" * 12]
        message.append(f"{self.__class__.__name__}")
        message.append("=" * 12)
        if self.has_input_address:
            message.append("---Input:")
            for name, value in self.__get_input_values():
                message.append(f"{name} | raw:{value.value} scaled:{value.scaled_value}")
        if self.has_holding_address:
            message.append("---Holding:")
            for name, value in self.__get_holding_values():
                message.append(f"{name} | raw:{value.value} scaled:{value.scaled_value}")
        if self.has_performance_calculators:
            message.append("---Calculations:")
            for name, value in self.__get_performance_calculators():
                message.append(f"{name} | raw:{value.value} scaled:{value.scaled_value}")
        return "\n".join(message)
