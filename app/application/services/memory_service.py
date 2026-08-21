from typing import Any, Dict, List


class MemoryService:
    def __init__(self) -> None:
        self._store: Dict[str, List[str]] = {}
        self._context_store: Dict[str, Dict[str, Any]] = {}

    def add_user_message(self, sender_id: str, text: str) -> None:
        history = self._store.setdefault(sender_id, [])
        history.append(f"user: {text}")
        self._trim_history(sender_id)

    def add_assistant_message(self, sender_id: str, text: str) -> None:
        history = self._store.setdefault(sender_id, [])
        history.append(f"assistant: {text}")
        self._trim_history(sender_id)

    def get_history(self, sender_id: str) -> List[str]:
        return self._store.get(sender_id, [])

    def get_context(self, sender_id: str) -> Dict[str, Any]:
        return dict(self._context_store.get(sender_id, {}))

    def update_context(self, sender_id: str, **values: Any) -> None:
        context = self._context_store.setdefault(sender_id, {})
        for key, value in values.items():
            if value is None:
                context.pop(key, None)
            else:
                context[key] = value

    def _trim_history(self, sender_id: str, max_items: int = 10) -> None:
        history = self._store.get(sender_id, [])
        if len(history) > max_items:
            self._store[sender_id] = history[-max_items:]
