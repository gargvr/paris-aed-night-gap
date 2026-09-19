"""Hand-checked readings of real Géo'DAE strings (Paris extract, 2026-09-19)."""
import numpy as np
import pytest

from aednight.hours import ALL, WEEKDAYS, parse_text, schedule_to_week, week_to_hours

MF = WEEKDAYS
H = lambda h, m=0: h * 60 + m  # noqa: E731


def s(days, *ranges):
    return {d: list(ranges) for d in days}


CASES = [
    ("L au V 9h/18h", MF, s(MF, (H(9), H(18)))),
    ("L au V 8h30/12h30 13h30/17h30", MF, s(MF, (H(8, 30), H(12, 30)), (H(13, 30), H(17, 30)))),
    ("8h-12h 13h-16h", MF, s(MF, (H(8), H(12)), (H(13), H(16)))),  # days from c_disp_j
    ("Lundi au vendredi 7h 19h", MF, s(MF, (H(7), H(19)))),
    ("L au V 7h30/19h S 8h/18h", MF, {**s(MF, (H(7, 30), H(19))), 5: [(H(8), H(18))]}),
    ("L au J 8h30/17h30 V 8h30/16h30", MF, {**s(range(4), (H(8, 30), H(17, 30))), 4: [(H(8, 30), H(16, 30))]}),
    ("L , Ma , Me , J 8h30/12h30 13h30/17h30 V 8h/30/12h30 13h30/16h30", MF,
     {**s(range(4), (H(8, 30), H(12, 30)), (H(13, 30), H(17, 30))), 4: [(H(8, 30), H(12, 30)), (H(13, 30), H(16, 30))]}),
    ("L au J 8h30/12h30 13h30/17h30 et V 16h30", MF,
     {**s(range(4), (H(8, 30), H(12, 30)), (H(13, 30), H(17, 30))), 4: [(H(8, 30), H(12, 30)), (H(13, 30), H(16, 30))]}),
    ("L à J 9h à 17h et le V 14h", MF, {**s(range(4), (H(9), H(17))), 4: [(H(9), H(14))]}),
    ("Horaires : 10:00 - 20:00", ALL, s(ALL, (H(10), H(20)))),
    ("mo-sa 10:00-20:00 su 11:00-20:00", ALL, {**s(range(6), (H(10), H(20))), 6: [(H(11), H(20))]}),
    ("7j/7 20h/8h", ALL, s(ALL, (H(20), H(8)))),
    ("Ma au D 12h/00h + selon évènements", ALL, s(range(1, 7), (H(12), H(0)))),
    ("7h45 à 16h09 du l au v", MF, s(MF, (H(7, 45), H(16, 9)))),
    ("6h/21h 7j/7", ALL, s(ALL, (H(6), H(21)))),
    ("L au V 24h/24", MF, s(MF, (0, 1440))),
    ("7j/7 L au V 7h/20h S D 7h/19h", ALL, {**s(MF, (H(7), H(20))), 5: [(H(7), H(19))], 6: [(H(7), H(19))]}),
    ("L au V 8h/22h30 + S D selon évènements", MF, s(MF, (H(8), H(22, 30)))),
    ("L Ma J V 7h-14h30 Me15h-20h  S 9h-13h / 15h30-19h30 D 9h-13h", ALL,
     {**s([0, 1, 3, 4], (H(7), H(14, 30))), 2: [(H(15), H(20))], 5: [(H(9), H(13)), (H(15, 30), H(19, 30))], 6: [(H(9), H(13))]}),
    ("L au J 9h à 13 et 14h à 18h et V de 9h à 13h et de 14h a 17h", MF,
     {**s(range(4), (H(9), H(13)), (H(14), H(18))), 4: [(H(9), H(13)), (H(14), H(17))]}),
    ("7j/7 8-23h", ALL, s(ALL, (H(8), H(23)))),
    ("L au V 8h-18-30", MF, s(MF, (H(8), H(18, 30)))),
    ("L au V 8h-12h / 14h16h", MF, s(MF, (H(8), H(12)), (H(14), H(16)))),
    ("Lau V de 9h/16h", MF, s(MF, (H(9), H(16)))),
    ("L au d 10h-21h", frozenset({2, 3, 4, 5, 6}), s(ALL, (H(10), H(21)))),  # text wins over field
    ("7J/7 18h a 1h", ALL, s(ALL, (H(18), H(1)))),
    ("L au j 8h30 à 12h30 puis 13h30 à 17h30 sauf V 16h30 code accès: 3078B", MF,
     {**s(range(4), (H(8, 30), H(12, 30)), (H(13, 30), H(17, 30))), 4: [(H(8, 30), H(12, 30)), (H(13, 30), H(16, 30))]}),
    ("07h15 à 11h45 et 13h à 18h l au j puis 17h le v et le s", MF,
     {**s(range(4), (H(7, 15), H(11, 45)), (H(13), H(18))), 4: [(H(7, 15), H(11, 45)), (H(13), H(17))], 5: [(H(7, 15), H(11, 45)), (H(13), H(17))]}),
    ("7j/7 9h/18h (7j/7 24h/24 uniquement pour les résidents)", ALL, s(ALL, (H(9), H(18)))),
    # from c_acc_complt
    ("Salle d'accueil du public : libre accès le lundi, mardi, Mercredi, Jeudi de 09h00 à 13h15\tpuis en vidéo-portier de 13h15-17h00, le Vendredi de 09h00 à 12h00",
     MF, {**s(range(4), (H(9), H(13, 15)), (H(13, 15), H(17))), 4: [(H(9), H(12))]}),
    ("accès de10h30 à 11h45 et de 18h30 à 19h45. Appareil à droite de l'entrée de l'église", frozenset({6}),
     s([6], (H(10, 30), H(11, 45)), (H(18, 30), H(19, 45)))),
    ("Ouvert de 9H à 12h30 et de 13h30 à 18h (vendredi 17h)", MF,
     {**s(MF, (H(9), H(12, 30)), (H(13, 30), H(18))), 4: [(H(9), H(12, 30)), (H(13, 30), H(17))]}),
    ("Gare accessible de 4h30 à 1h", ALL, s(ALL, (H(4, 30), H(1)))),
    ("Accessible à l'Accueil RDC du Lundi au Samedi de 6h00 à 22h00", MF, s(range(6), (H(6), H(22)))),
    ("Horaires ouverture des bureaux : 09h 13h - 14h 18h / Interphone ARIHM, puis 1er étage droite", MF,
     s(MF, (H(9), H(13)), (H(14), H(18)))),
    ("Lundi : 10h00 - 12h00 ; Mardi : 15h00 - 17h00 ; Vendredi : 18h00 - 20h00 ; Accès parfois possible", MF,
     {0: [(H(10), H(12))], 1: [(H(15), H(17))], 4: [(H(18), H(20))]}),
    ("bâtiment cour - ouvert tous les jours de 9h à 22h y compris les jours fériés", ALL, s(ALL, (H(9), H(22)))),
    ("Ouvert toute l'année de 9h à 23h45.", ALL, s(ALL, (H(9), H(23, 45)))),
    ("ouvert de 5h à minuit, accès porte sonnette Radio Notre Dame", ALL, s(ALL, (H(5), H(24)))),
    ("Le DAE sera disponible les jours ouvrés, du lundi au vendredi, de 9 h 00 à 17 h 30.", MF, s(MF, (H(9), H(17, 30)))),
    ("Lundi au vendredi 7h 19h", MF, s(MF, (H(7), H(19)))),
    ("Horaires d'accessibilité de 6h30 à 23h du lundi au vendredi et de 8h à 19h du samedi au dimanche.", ALL,
     {**s(MF, (H(6, 30), H(23))), 5: [(H(8), H(19))], 6: [(H(8), H(19))]}),
    ("Il s'agit d'un restaurant ouvert du mercredi au dimanche de 7h à 2h du matin.", ALL, s(range(2, 7), (H(7), H(2)))),
    ("Cinéma ouvert au public tous les jours entre 13h et 00h.", ALL, s(ALL, (H(13), H(0)))),
    ("L 7h30/8h30 12h/13h30 Ma 12h/13h30 17h/20h", ALL,
     {0: [(H(7, 30), H(8, 30)), (H(12), H(13, 30))], 1: [(H(12), H(13, 30)), (H(17), H(20))]}),
    ("Mo-Fr 09:00-18:00", MF, s(MF, (H(9), H(18)))),
]


@pytest.mark.parametrize("text,fdays,expected", CASES, ids=[c[0][:40] for c in CASES])
def test_parse(text, fdays, expected):
    p = parse_text(text, fdays)
    assert p.ok, p.note
    assert p.schedule == expected


def test_wrap_past_midnight_hits_next_day():
    hours = week_to_hours(schedule_to_week({6: [(H(20), H(8))]})).reshape(7, 24)
    assert hours[6, 20:].all() and hours[0, :8].all() and not hours[0, 8]


def test_midpoint_sampling():
    hours = week_to_hours(schedule_to_week({0: [(H(8, 30), H(17, 30))]})).reshape(7, 24)
    assert np.flatnonzero(hours[0]).tolist() == list(range(8, 17))


def test_unparseable_reports_failure():
    assert not parse_text("L au V 9h", frozenset(range(5))).ok
