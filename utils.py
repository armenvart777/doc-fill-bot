"""Utility functions: date formatting, initials, number-to-words in Russian."""

from datetime import datetime


MONTHS_RU = {
    1: "января", 2: "февраля", 3: "марта", 4: "апреля",
    5: "мая", 6: "июня", 7: "июля", 8: "августа",
    9: "сентября", 10: "октября", 11: "ноября", 12: "декабря",
}


def format_today() -> str:
    """Return today's date in Russian format: '22 февраля 2026'."""
    ...


def get_initials(full_name: str) -> str:
    """Convert 'Чернявская Мария Павловна' → 'Чернявская М.П.'"""
    ...


# --- Number to words in Russian ---

ONES = [
    "", "один", "два", "три", "четыре", "пять",
    "шесть", "семь", "восемь", "девять", "десять",
    "одиннадцать", "двенадцать", "тринадцать", "четырнадцать", "пятнадцать",
    "шестнадцать", "семнадцать", "восемнадцать", "девятнадцать",
]

ONES_FEM = [
    "", "одна", "две", "три", "четыре", "пять",
    "шесть", "семь", "восемь", "девять",
]

TENS = [
    "", "", "двадцать", "тридцать", "сорок", "пятьдесят",
    "шестьдесят", "семьдесят", "восемьдесят", "девяносто",
]

HUNDREDS = [
    "", "сто", "двести", "триста", "четыреста", "пятьсот",
    "шестьсот", "семьсот", "восемьсот", "девятьсот",
]

THOUSANDS = ("тысяча", "тысячи", "тысяч")
MILLIONS = ("миллион", "миллиона", "миллионов")


def _plural_form(n: int, forms: tuple) -> str:
    """Select correct Russian plural form for a number."""
    ...


def _three_digits(n: int, feminine: bool = False) -> str:
    """Convert a 3-digit number to Russian words."""
    ...


def num_to_words(n: int) -> str:
    """Convert integer to Russian words. Supports up to 999,999,999."""
    ...
