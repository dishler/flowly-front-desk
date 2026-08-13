from typing import Set


class DedupService:
    def __init__(self) -> None:
        self._seen_message_ids: Set[str] = set()
        self._processing_message_ids: Set[str] = set()

    def is_duplicate(self, message_mid: str) -> bool:
        return (
            message_mid in self._seen_message_ids
            or message_mid in self._processing_message_ids
        )

    def claim_processing(self, message_mid: str) -> bool:
        if self.is_duplicate(message_mid):
            return False

        self._processing_message_ids.add(message_mid)
        return True

    def mark_processed(self, message_mid: str) -> None:
        self._seen_message_ids.add(message_mid)
        self._processing_message_ids.discard(message_mid)

    def release_processing(self, message_mid: str) -> None:
        self._processing_message_ids.discard(message_mid)
