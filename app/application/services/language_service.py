class LanguageService:
    def detect_language(self, text: str) -> str:
        normalized = text.strip()

        if self._contains_cyrillic(normalized):
            return "uk"

        if self._contains_cjk(normalized):
            return "zh"

        return "en"

    @staticmethod
    def _contains_cyrillic(text: str) -> bool:
        return any("а" <= ch.lower() <= "я" or ch.lower() == "ї" or ch.lower() == "є" or ch.lower() == "ґ" for ch in text)

    @staticmethod
    def _contains_cjk(text: str) -> bool:
        return any("\u4e00" <= ch <= "\u9fff" for ch in text)
        