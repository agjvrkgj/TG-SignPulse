from __future__ import annotations

import json
import logging
from pathlib import Path
from typing import Any, Dict, List

from backend.core.config import get_settings
from backend.services.config import get_config_service
from backend.services.telegram import get_telegram_service
from backend.utils.time import utc_now_iso_z

logger = logging.getLogger("backend.device_monitor")


class DeviceMonitorService:
    """Detect newly added Telegram authorized devices for all accounts."""

    def __init__(self) -> None:
        self.settings = get_settings()
        self.workdir = self.settings.resolve_workdir()
        self.state_file = self.workdir / ".device_monitor_state.json"

    def _load_state(self) -> Dict[str, Any]:
        if not self.state_file.exists():
            return {"accounts": {}}
        try:
            data = json.loads(self.state_file.read_text(encoding="utf-8"))
            if isinstance(data, dict):
                accounts = data.get("accounts")
                if not isinstance(accounts, dict):
                    data["accounts"] = {}
                return data
        except (json.JSONDecodeError, OSError):
            pass
        return {"accounts": {}}

    def _save_state(self, state: Dict[str, Any]) -> None:
        try:
            self.state_file.parent.mkdir(parents=True, exist_ok=True)
            self.state_file.write_text(
                json.dumps(state, ensure_ascii=False, indent=2), encoding="utf-8"
            )
        except OSError as exc:
            logger.warning("Failed to save device monitor state: %s", exc)

    @staticmethod
    def _device_key(device: Dict[str, Any]) -> str:
        value = str(device.get("hash") or "").strip()
        if value:
            return value
        parts = [
            device.get("device_model"),
            device.get("platform"),
            device.get("app_name"),
            device.get("app_version"),
            device.get("date_created"),
            device.get("ip"),
        ]
        return "|".join(str(part or "") for part in parts)

    @staticmethod
    def _format_device(account_name: str, device: Dict[str, Any]) -> str:
        title = " ".join(
            part
            for part in [
                str(device.get("device_model") or "").strip(),
                str(device.get("platform") or "").strip(),
            ]
            if part
        ) or "未知设备"
        app = " ".join(
            part
            for part in [
                str(device.get("app_name") or "").strip(),
                str(device.get("app_version") or "").strip(),
            ]
            if part
        ) or "未知 App"
        location = " ".join(
            part
            for part in [
                str(device.get("ip") or "").strip(),
                str(device.get("country") or "").strip(),
                str(device.get("region") or "").strip(),
            ]
            if part
        ) or "未知位置"
        return (
            f"账号: {account_name}\n"
            f"设备: {title}\n"
            f"App: {app}\n"
            f"位置: {location}\n"
            f"登录时间: {device.get('date_created') or 'unknown'}\n"
            f"最近活跃: {device.get('date_active') or 'unknown'}\n"
            f"当前会话: {'是' if device.get('current') else '否'}"
        )

    async def _send_new_device_notification(self, new_devices: List[Dict[str, Any]]) -> None:
        settings = get_config_service().get_global_settings()
        if not settings.get("telegram_bot_notify_enabled"):
            logger.info("Telegram bot notification disabled; skip new device alert")
            return

        bot_token = (settings.get("telegram_bot_token") or "").strip()
        chat_id = (settings.get("telegram_bot_chat_id") or "").strip()
        if not bot_token or not chat_id:
            logger.warning("Telegram bot notification is not configured")
            return

        from backend.services.push_notifications import _as_int_or_none, send_telegram_bot_message

        sections = [
            "⚠️ TG-SignPulse 检测到 Telegram 账号新增授权设备",
            f"检测时间: {utc_now_iso_z()}",
            "",
        ]
        for index, item in enumerate(new_devices, 1):
            sections.append(f"新增设备 #{index}")
            sections.append(self._format_device(item["account_name"], item["device"]))
            sections.append("")
        sections.append("如果不是你本人操作，请尽快到账号管理 → 设备中踢下线，并检查账号安全。")

        await send_telegram_bot_message(
            bot_token=bot_token,
            chat_id=chat_id,
            text="\n".join(sections),
            message_thread_id=_as_int_or_none(settings.get("telegram_bot_message_thread_id")),
        )

    async def scan(self, force_notify: bool = False) -> Dict[str, Any]:
        settings = get_config_service().get_global_settings()
        enabled = bool(settings.get("device_change_detection_enabled", True))
        if not enabled and not force_notify:
            return {
                "success": True,
                "enabled": False,
                "checked": 0,
                "new_devices": 0,
                "failed": 0,
                "baseline_only": False,
                "results": [],
            }

        state = self._load_state()
        account_state = state.setdefault("accounts", {})
        service = get_telegram_service()
        accounts = service.list_accounts(force_refresh=True)

        results: List[Dict[str, Any]] = []
        new_devices: List[Dict[str, Any]] = []
        checked = failed = 0
        baseline_only = False

        for item in accounts:
            account_name = str(item.get("name") or "").strip()
            if not account_name:
                continue
            try:
                devices = await service.list_account_devices(account_name)
                checked += 1
                known = account_state.setdefault(account_name, {})
                known_devices = known.setdefault("devices", {})
                first_seen = not bool(known_devices)
                if first_seen:
                    baseline_only = True

                current_devices: Dict[str, Dict[str, Any]] = {}
                account_new = 0
                for device in devices:
                    key = self._device_key(device)
                    if not key:
                        continue
                    current_devices[key] = device
                    if not first_seen and key not in known_devices:
                        account_new += 1
                        new_devices.append({"account_name": account_name, "device": device})

                known["devices"] = current_devices
                known["last_scan_at"] = utc_now_iso_z()
                known["last_error"] = None
                results.append(
                    {
                        "account_name": account_name,
                        "status": "ok",
                        "devices": len(current_devices),
                        "new_devices": account_new,
                        "baseline": first_seen,
                    }
                )
            except Exception as exc:
                failed += 1
                known = account_state.setdefault(account_name, {})
                known["last_scan_at"] = utc_now_iso_z()
                known["last_error"] = str(exc)
                logger.warning("Device monitor scan failed for %s: %s", account_name, exc)
                results.append(
                    {
                        "account_name": account_name,
                        "status": "failed",
                        "message": str(exc),
                    }
                )

        state["last_scan_at"] = utc_now_iso_z()
        self._save_state(state)

        if new_devices:
            try:
                await self._send_new_device_notification(new_devices)
            except Exception as exc:
                logger.warning("Failed to send new device notification: %s", exc)

        return {
            "success": failed == 0,
            "enabled": enabled,
            "checked": checked,
            "new_devices": len(new_devices),
            "failed": failed,
            "baseline_only": baseline_only and not new_devices,
            "results": results,
        }

    def get_state(self) -> Dict[str, Any]:
        state = self._load_state()
        accounts = state.get("accounts")
        if not isinstance(accounts, dict):
            accounts = {}
        return {
            "last_scan_at": state.get("last_scan_at"),
            "accounts": [
                {
                    "account_name": account_name,
                    "last_scan_at": info.get("last_scan_at") if isinstance(info, dict) else None,
                    "last_error": info.get("last_error") if isinstance(info, dict) else None,
                    "device_count": len(info.get("devices") or {}) if isinstance(info, dict) else 0,
                }
                for account_name, info in sorted(accounts.items())
            ],
        }


_device_monitor_service: DeviceMonitorService | None = None


def get_device_monitor_service() -> DeviceMonitorService:
    global _device_monitor_service
    if _device_monitor_service is None:
        _device_monitor_service = DeviceMonitorService()
    return _device_monitor_service
