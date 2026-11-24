import random

def mock_weather_report(args: dict, **_):
    city = args.get('city', 'Unknown City')
    conditions = ['Sunny', 'Cloudy', 'Rainy', 'Stormy', 'Snowy', 'Windy', 'Foggy']
    temps = list(range(-5, 36))
    humidity = random.randint(20, 95)
    temp = random.choice(temps)
    condition = random.choice(conditions)
    report = f"Weather for {city}: {condition}, {temp}°C, Humidity: {humidity}%"
    return report
