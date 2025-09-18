import math

def calculate_distance(lat1, lon1, lat2, lon2) -> int:
    R = 6371000
    phi1, phi2 = map(math.radians, [lat1, lat2])
    delta_phi = math.radians(lat2 - lat1)
    delta_lambda = math.radians(lon2 - lon1)
    a = math.sin(delta_phi / 2)**2 + math.cos(phi1) * math.cos(phi2) * math.sin(delta_lambda / 2)**2
    c = 2 * math.atan2(math.sqrt(a), math.sqrt(1 - a))
    return int(R * c)

def calculate_bearing(lat1, lon1, lat2, lon2) -> float:
    lat1, lon1, lat2, lon2 = map(math.radians, [lat1, lon1, lat2, lon2])
    dLon = lon2 - lon1
    y = math.sin(dLon) * math.cos(lat2)
    x = math.cos(lat1) * math.sin(lat2) - math.sin(lat1) * math.cos(lat2) * math.cos(dLon)
    return (math.degrees(math.atan2(y, x)) + 360) % 360

def bearing_to_direction(bearing: float) -> str:
    """Преобразует градусы в ключ направления для перевода."""
    val = int((bearing / 45) + 0.5)
    direction_keys = [
        "direction_north", "direction_northeast", "direction_east", "direction_southeast",
        "direction_south", "direction_southwest", "direction_west", "direction_northwest"
    ]
    return direction_keys[val % 8]
