from __future__ import annotations

import json
import logging
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any, Dict, List

from backend.core.config import get_settings
from backend.services.config import get_config_service
from backend.services.telegram import get_telegram_service
from backend.utils.time import utc_now_iso_z

logger = logging.getLogger("backend.device_keepalive")


class DeviceKeepaliveService:
    """Keep Telegram account sessions active with lightweight periodic checks."""

    def __init__(self) -> None:
        self.settings = get_settings()
        self.workdir = self.settings.resolve_workdir()
        self.state_file = self.workdir / ".device_keepalive_state.json"

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
            logger.warning("Failed to save device keepalive state: %s", exc)

    @staticmethod
    def _parse_time(value: Any) -> datetime | None:
        if not value:
            return None
        try:
            text = str(value).replace("Z", "+00:00")
            parsed = datetime.fromisoformat(text)
            if parsed.tzinfo is None:
                parsed = parsed.replace(tzinfo=UTC)
            return parsed.astimezone(UTC)
        except ValueError:
            return None

    async def run_due(self, force: bool = False) -> Dict[str, Any]:
        config = get_config_service().get_global_settings()
        enabled = bool(config.get("device_keepalive_enabled", True))
        interval_days = int(config.get("device_keepalive_interval_days") or 30)
        interval_days = max(1, min(interval_days, 170))

        if not enabled and not force:
            return {
                "success": True,
                "enabled": False,
                "checked": 0,
                "kept_alive": 0,
                "skipped": 0,
                "failed": 0,
                "results": [],
            }

        state = self._load_state()
        account_state = state.setdefault("accounts", {})
        now = datetime.now(UTC)
        cutoff = now - timedelta(days=interval_days)

        service = get_telegram_service()
        accounts = service.list_accounts(force_refresh=True)
        results: List[Dict[str, Any]] = []
        kept_alive = skipped = failed = 0

        for item in accounts:
            account_name = str(item.get("name") or "").strip()
            if not account_name:
                continue

            last_ok = self._parse_time(account_state.get(account_name, {}).get("last_ok_at"))
            if not force and last_ok and last_ok > cutoff:
                skipped += 1
                results.append(
                    {
                        "account_name": account_name,
                        "status": "skipped",
                        "message": "not due",
                        "last_ok_at": last_ok.isoformat().replace("+00:00", "Z"),
                    }
                )
                continue

            try:
                status = await service.check_account_status(
                    account_name, timeout_seconds=12.0, no_updates=True
                )
                ok = bool(status.get("ok"))
                entry = account_state.setdefault(account_name, {})
                entry["last_attempt_at"] = utc_now_iso_z()
                if ok:
                    entry["last_ok_at"] = utc_now_iso_z()
                    entry["last_error"] = None
                    kept_alive += 1
                    results.append(
                        {
                            "account_name": account_name,
                            "status": "ok",
                            "message": "keepalive ok",
                        }
                    )
                else:
                    message = str(status.get("message") or status.get("code") or "failed")
                    entry["last_error"] = message
                    failed += 1
                    results.append(
                        {
                            "account_name": account_name,
                            "status": "failed",
                            "message": message,
                        }
                    )
            except Exception as exc:
                entry = account_state.setdefault(account_name, {})
                entry["last_attempt_at"] = utc_now_iso_z()
                entry["last_error"] = str(exc)
                failed += 1
                logger.warning("Device keepalive failed for %s: %s", account_name, exc)
                results.append(
                    {
                        "account_name": account_name,
                        "status": "failed",
                        "message": str(exc),
                    }
                )

        state["last_run_at"] = utc_now_iso_z()
        self._save_state(state)
        return {
            "success": failed == 0,
            "enabled": enabled,
            "checked": kept_alive + failed,
            "kept_alive": kept_alive,
            "skipped": skipped,
            "failed": failed,
            "interval_days": interval_days,
            "results": results,
        }


_device_keepalive_service: DeviceKeepaliveService | None = None


def get_device_keepalive_service() -> DeviceKeepaliveService:
    global _device_keepalive_service
    if _device_keepalive_service is None:
        _device_keepalive_service = DeviceKeepaliveService()
    return _device_keepalive_service
