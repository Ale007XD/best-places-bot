# scripts/test_venue_filter.py
"""Проверка контуров фильтрации нецелевых заведений (venue_filter)."""

from bot.utils.venue_filter import drop_non_food, extract_osm_tags, reject_reason


def main() -> None:
    # Контур 1: стоп-слова и стоп-фразы
    assert reject_reason({"name": "TT Pilates Nha Trang"}) == "name_non_food"
    assert reject_reason({"name": "CLB HOANG GIA"}) == "name_non_food"
    assert reject_reason({"name": "Nha Khoa Sài Gòn"}) == "name_non_food"
    assert reject_reason({"name": "Студия пилатеса Баланс"}) == "name_non_food"

    # Swarm-спам Foursquare: эмодзи, частные чекины, нецелевые подкатегории
    assert (
        reject_reason({"name": "Лакшери Хата Бухая Соната😂👌🎉", "primary_type": "Бар"})
        == "emoji_spam"
    )
    assert (
        reject_reason({"name": "на площадке 👯🍇🍇🍇", "primary_type": "Виноградник"})
        == "emoji_spam"
    )
    assert reject_reason({"name": "Лакшери Хата Бухая Соната"}) == "swarm_junk"
    assert reject_reason({"name": "на площадке", "vicinity": None}) == "swarm_junk"
    assert reject_reason({"name": "у Вовы", "vicinity": None, "rating": None}) == "casual_checkin"
    assert (
        reject_reason({"name": "У Вовы", "vicinity": "ул. Ленина 5", "rating": None})
        is None
    )
    assert (
        reject_reason({"name": "Солнечная долина", "primary_type": "Виноградник"})
        == "non_food_type"
    )
    assert (
        reject_reason({
            "name": "Wine & Bar",
            "primary_type": "Виноградник",
            "fsq_categories": ["Виноградник", "Бар"],
        })
        is None
    )

    # Гастро-якорь перевешивает стоп-слово
    assert reject_reason({"name": "Yoga Cafe"}) is None
    assert reject_reason({"name": "Sunrise Hotel Rooftop Bar"}) is None
    assert reject_reason({"name": "Phở Hòa"}) is None

    # Контур 2: конфликт категорий Geoapify и тегов OSM
    assert (
        reject_reason({
            "name": "TT Studio",
            "raw_categories": ["catering.restaurant", "sport.fitness"],
        })
        == "tag_conflict"
    )
    assert (
        reject_reason({
            "name": "Minh Khai 02",
            "osm_tags": {"amenity": "restaurant", "leisure": "fitness_centre"},
        })
        == "tag_conflict"
    )

    # Наличие cuisine=* или гастро-якоря защищает от ложного отсева в контуре 2
    assert (
        reject_reason({
            "name": "Lotus",
            "raw_categories": ["catering.restaurant", "accommodation.hotel"],
            "osm_tags": {"cuisine": "vietnamese"},
        })
        is None
    )
    assert (
        reject_reason({
            "name": "Lotus Coffee",
            "raw_categories": ["catering.cafe", "accommodation.hotel"],
        })
        is None
    )

    # extract_osm_tags отбрасывает лишние поля
    assert extract_osm_tags({"osm_id": 123, "cuisine": "vietnamese", "sport": "pilates"}) == {
        "cuisine": "vietnamese",
        "sport": "pilates",
    }

    # drop_non_food сохраняет только валидные карточки
    kept = drop_non_food([
        {"name": "TT Pilates Nha Trang"},
        {"name": "Лакшери Хата Бухая Соната😂👌🎉", "primary_type": "Бар"},
        {"name": "на площадке 👯🍇🍇🍇", "primary_type": "Виноградник"},
        {"name": "Cà Phê Sữa Đá"},
        {"name": "", "unnamed": True, "osm_tags": {"amenity": "cafe"}},
    ])
    assert [p["name"] for p in kept] == ["Cà Phê Sữa Đá", ""]
    print("OK: venue_filter")


if __name__ == "__main__":
    main()
