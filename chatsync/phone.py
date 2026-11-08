import phonenumbers

MIN_LENIENT_DIGITS = 10


def is_acceptable(number) -> bool:
	if phonenumbers.is_valid_number(number):
		return True
	return phonenumbers.is_possible_number(number) and len(str(number.national_number)) >= MIN_LENIENT_DIGITS


def normalize(raw: str, region: str) -> str | None:
	try:
		number = phonenumbers.parse(raw, region)
	except phonenumbers.NumberParseException:
		return None
	if not is_acceptable(number):
		return None
	return phonenumbers.format_number(number, phonenumbers.PhoneNumberFormat.E164)
