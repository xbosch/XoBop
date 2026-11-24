def fahrenheit_to_celsius(args: dict, **_):
    f = args['fahrenheit']
    c = (f - 32) * 5.0 / 9.0
    return str(round(c, 2))