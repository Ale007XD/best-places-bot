# bot/utils/attribution.py
"""Обязательные указания источника данных (лицензии провайдеров)."""

_BY_PROVIDER = {
    "geoapify": "🗺 Powered by Geoapify · © OpenStreetMap contributors",
    "overpass": "🗺 © OpenStreetMap contributors",
    "fsq": "📍 Powered by Foursquare",
}


def attribution(provider: str, places: list[dict] | None = None) -> str:
    """Строка атрибуции для выдачи; пусто, если указывать нечего.

    Если провайдер не известен (старые данные), но в выдаче есть места OSM — указываем OSM.
    """
    if provider in _BY_PROVIDER:
        return _BY_PROVIDER[provider]
    if any(p.get("source") == "osm" for p in places or []):
        return _BY_PROVIDER["overpass"]
    return ""
