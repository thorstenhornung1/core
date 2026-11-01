"""Base entity for Daikin."""

from collections.abc import Coroutine
from typing import Any

from pydaikin.exceptions import DaikinException

from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers.device_registry import CONNECTION_NETWORK_MAC, DeviceInfo
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from .const import DOMAIN
from .coordinator import DaikinCoordinator


class DaikinEntity(CoordinatorEntity[DaikinCoordinator]):
    """Base entity for Daikin."""

    _attr_has_entity_name = True

    def __init__(self, coordinator: DaikinCoordinator) -> None:
        """Initialize the entity."""
        super().__init__(coordinator)
        self.device = coordinator.device
        info = self.device.values
        self._attr_device_info = DeviceInfo(
            connections={(CONNECTION_NETWORK_MAC, self.device.mac)},
            manufacturer="Daikin",
            model=info.get("model"),
            name=info.get("name"),
            sw_version=info.get("ver", "").replace("_", "."),
        )

    async def _async_send_command(self, command: Coroutine[Any, Any, None]) -> None:
        """Send a command to the device and refresh its state."""
        try:
            await command
        except DaikinException as err:
            raise HomeAssistantError(
                translation_domain=DOMAIN,
                translation_key="command_failed",
                translation_placeholders={"error": str(err)},
            ) from err
        finally:
            # Also after a rejected command, so the state reflects the device
            await self.coordinator.async_refresh()
