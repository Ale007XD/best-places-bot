import math

def calculate_distance(lat1, lon1, lat2, lon2) -> int:
    """
    Вычисляет расстояние между двумя точками в метрах (формула Гаверсинуса).
    """
    R = 6371000  # Радиус Земли в метрах
    phi1 = math.radians(lat1)
    phi2 = math.radians(lat2)
    delta_phi = math.radians(lat2 - lat1)
    delta_lambda = math.radians(lon2 - lon1)

    a = math.sin(delta_phi / 2)**2 + math.cos(phi1) * math.cos(phi2) * math.sin(delta_lambda / 2)**2
    c = 2 * math.atan2(math.sqrt(a), math.sqrt(1 - a))

    return int(R * c)

def calculate_bearing(lat1, lon1, lat2, lon2) -> float:
    """
    Вычисляет азимут (направление) от точки 1 к точке 2 в градусах.
    """
    lat1, lon1, lat2, lon2 = map(math.radians, [lat1, lon1, lat2, lon2])

    dLon = lon2 - lon1
    y = math.sin(dLon) * math.cos(lat2)
    x = math.cos(lat1) * math.sin(lat2) - math.sin(lat1) * math.cos(lat2) * math.cos(dLon)
    
    bearing = math.atan2(y, x)
    return (math.degrees(bearing) + 360) % 360

def bearing_to_direction(bearing: float) -> str:
    """
    Преобразует градусы азимута в текстовое описание направления.
    """
    val = int((bearing / 45) + 0.5)
    directions = [
        "на север", "на северо-восток", "на восток", "на юго-восток",
        "на юг", "на юго-запад", "на запад", "на северо-запад"
    ]
    return directions[val % 8]
