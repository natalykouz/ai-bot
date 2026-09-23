"""Тесты этапа 2 Letteros -> UniSender: миграция целого письма
(letteros_migrate.py). Без сети, без изменения файлов проекта. Запуск:
python mail_project/test_letteros_migrate.py

Обновление: letteros_recognition теперь умеет module-level recognition для
"Шапки"/"Подвалы" (element=None, см. MODULE_LEVEL_ELIGIBLE_MODULES) -- письмо
с настоящей шапкой/подвалом больше не стопорится на "не удалось однозначно
распознать" уже на этапе распознавания. Но у "Подвалы" в
letteros_adapt.KNOWN_ADAPTATION_EXCEPTIONS есть запись ("Подвалы", "Школа 1"),
и без конкретного element нельзя ни подтвердить, ни безопасно исключить, что
это она -- поэтому module-level "Подвалы" сейчас ВСЕГДА REQUIRES_MANUAL_REVIEW
(см. letteros_adapt.py, ModuleLevelAdaptationTests в test_letteros_adapt.py).
Итог: письмо с настоящим подвалом сегодня всё ещё останавливает
migrate_letter() -- но по другой, куда более узкой и понятной причине
(конкретно подвал требует ручной проверки), а не из-за общей неоднозначности
распознавания. Это задокументировано классом FooterStillStopsButSaferNowTests
ниже, а не скрыто. Тесты на "вставка перед подвалом"/"невозможно определить
точку вставки" по-прежнему проверяют _split_schedule_and_determine_insertion()
напрямую -- это не связано с ограничением выше, просто самый прямой способ
протестировать эту конкретную логику в изоляции.
"""

import re
import tempfile
import unittest
from pathlib import Path

import generation
import letteros_migrate as lm
import letteros_recognition as lr
from letteros_recognition import RecognizedComponent

BASE_DIR = Path(__file__).resolve().parent
REAL_LETTERS_DIR = BASE_DIR / "letteros htmls"

_LETTEROS_ATTR_RE = re.compile(r'\s+(?:letteros-[a-z-]+|telegram-?)="[^"]*"', re.IGNORECASE)


def _strip_letteros_attrs(html: str) -> str:
    """Та же тестовая утилита, что и в test_letteros_recognition.py /
    test_letteros_adapt.py -- имитирует реальный экспорт письма из Letteros."""
    return _LETTEROS_ATTR_RE.sub("", html)


def _clean(lib: dict, module: str, element: str) -> str:
    return _strip_letteros_attrs(lib[(module, element)].html)


def _join(*fragments: str) -> str:
    return "\n".join(fragments)


def _write_schedule_txt(tmp_dir: Path, *, title="Тестовая лекция миграции", time_="19:00",
                         date="18.09.2026", link="https://example.com/lecture") -> Path:
    path = tmp_dir / "SCHEDULE.txt"
    path.write_text(
        f"DATE:{date}\nEVENT\ntime:{time_}\ntitle:{title}\nlink:{link}\n",
        encoding="utf-8",
    )
    return path


def _fake_match(module: str, element: str, order: int) -> RecognizedComponent:
    """Для юнит-тестов чистой логики _split_schedule_and_determine_insertion()
    -- не проходит через recognize_components(), не требует реального HTML."""
    return RecognizedComponent(module, element, order, 0, 0, f"<tr><!-- {module}/{element} --></tr>", "test")


class SuccessfulMigrationTests(unittest.TestCase):
    """"Счастливый путь" собран из компонентов, заведомо однозначно
    распознаваемых поодиночке (проверено на предыдущем этапе) -- шапки/
    подвалы сюда намеренно не включены, см. модульный docstring."""

    @classmethod
    def setUpClass(cls):
        cls.letteros_lib = lr.load_letteros_library()
        cls.tmpdir = tempfile.TemporaryDirectory()
        cls.schedule_path = _write_schedule_txt(Path(cls.tmpdir.name))

    @classmethod
    def tearDownClass(cls):
        cls.tmpdir.cleanup()

    def test_order_preserved(self):
        letter = _join(
            _clean(self.letteros_lib, "Текстовые блоки", "Текст 16px"),
            _clean(self.letteros_lib, "Мероприятия", "Расписание 1"),
            _clean(self.letteros_lib, "Кнопки", "Зелёная кнопка на всю ширину"),
            _clean(self.letteros_lib, "Дополнительные Баннеры", "Вариант 1"),
        )
        result = lm.migrate_letter(letter, self.schedule_path, letteros_library=self.letteros_lib)
        self.assertEqual(
            result.components,
            [
                ("Текстовые блоки", "Текст 16px"),
                ("Кнопки", "Зелёная кнопка на всю ширину"),
                ("Дополнительные Баннеры", "Вариант 1"),
            ],
        )
        self.assertEqual(result.schedule_inserted_at, 1)  # на месте старого расписания

    def test_content_unchanged_after_adaptation(self):
        letter = _join(
            _clean(self.letteros_lib, "Текстовые блоки", "Текст 16px"),
            _clean(self.letteros_lib, "Мероприятия", "Расписание 1"),
            _clean(self.letteros_lib, "Кнопки", "Зелёная кнопка на всю ширину"),
        )
        result = lm.migrate_letter(letter, self.schedule_path, letteros_library=self.letteros_lib)
        # текст кнопки - образец из самой библиотеки, adapt_component его не трогает
        self.assertIn("ЗАПИСАТЬСЯ", result.html)
        href_before = re.findall(r'href="([^"]*)"', self.letteros_lib[("Кнопки", "Зелёная кнопка на всю ширину")].html)
        self.assertTrue(href_before)
        for href in href_before:
            self.assertIn(f'href="{href}"', result.html)

    def test_image_and_background_channels_unchanged(self):
        letter = _join(
            _clean(self.letteros_lib, "Дополнительные Баннеры", "Вариант 1"),
            _clean(self.letteros_lib, "Мероприятия", "Расписание 1"),
        )
        result = lm.migrate_letter(letter, self.schedule_path, letteros_library=self.letteros_lib)
        src_before = re.findall(r'<img[^>]*\bsrc="([^"]*)"', self.letteros_lib[("Дополнительные Баннеры", "Вариант 1")].html)
        self.assertTrue(src_before)
        for src in src_before:
            self.assertIn(f'src="{src}"', result.html)

    def test_schedule_replaced_with_new_canonical_schedule(self):
        old_schedule_html = self.letteros_lib[("Мероприятия", "Расписание 1")].html
        self.assertIn("Дом переехал", old_schedule_html)  # sanity: образец старого расписания
        letter = _join(
            _clean(self.letteros_lib, "Текстовые блоки", "Текст 16px"),
            _strip_letteros_attrs(old_schedule_html),
            _clean(self.letteros_lib, "Кнопки", "Зелёная кнопка на всю ширину"),
        )
        result = lm.migrate_letter(letter, self.schedule_path, letteros_library=self.letteros_lib)
        self.assertNotIn("Дом переехал", result.html)  # старое расписание не перенесено
        self.assertIn("Тестовая лекция миграции", result.html)  # новое расписание вставлено
        self.assertIn("19:00", result.html)
        self.assertIn("ЗАПИСАТЬСЯ", result.html)  # остальные блоки на месте


class UnresolvedFragmentStopsTests(unittest.TestCase):
    """Требование: неизвестный/неоднозначный фрагмент -> остановка, не
    угадывание. Используются реальные Letteros-письма проекта (все 4 --
    ни одно не проходит полностью через recognize_components() без
    unresolved-фрагментов, см. диагностику к предыдущему изменению)."""

    @classmethod
    def setUpClass(cls):
        if not REAL_LETTERS_DIR.is_dir():
            raise unittest.SkipTest(f"каталог с реальными письмами не найден: {REAL_LETTERS_DIR}")
        cls.letteros_lib = lr.load_letteros_library()
        cls.tmpdir = tempfile.TemporaryDirectory()
        cls.schedule_path = _write_schedule_txt(Path(cls.tmpdir.name))
        cls.files = sorted(REAL_LETTERS_DIR.glob("*.html"))
        if not cls.files:
            raise unittest.SkipTest(f"в {REAL_LETTERS_DIR} нет .html файлов")

    @classmethod
    def tearDownClass(cls):
        cls.tmpdir.cleanup()

    def test_all_four_real_letters_stop_with_migration_error(self):
        for fp in self.files:
            raw = fp.read_text(encoding="utf-8")
            with self.assertRaises(lm.MigrationError, msg=fp.name):
                lm.migrate_letter(raw, self.schedule_path, letteros_library=self.letteros_lib)

    def test_synthetic_unrecognized_gap_between_known_blocks_stops(self):
        letter = _join(
            _clean(self.letteros_lib, "Текстовые блоки", "Текст 16px"),
            "<p>совершенно случайный HTML не из библиотеки компонентов</p>",
            _clean(self.letteros_lib, "Кнопки", "Зелёная кнопка на всю ширину"),
        )
        with self.assertRaises(lm.MigrationError) as ctx:
            lm.migrate_letter(letter, self.schedule_path, letteros_library=self.letteros_lib)
        self.assertIn("нераспознанный участок", str(ctx.exception))


class RequiresManualReviewStopsTests(unittest.TestCase):
    """Требование: компонент из KNOWN_ADAPTATION_EXCEPTIONS -> остановка всей
    миграции, а не тихая выдача потенциально некорректного результата."""

    @classmethod
    def setUpClass(cls):
        cls.letteros_lib = lr.load_letteros_library()
        cls.tmpdir = tempfile.TemporaryDirectory()
        cls.schedule_path = _write_schedule_txt(Path(cls.tmpdir.name))

    @classmethod
    def tearDownClass(cls):
        cls.tmpdir.cleanup()

    def test_known_exception_component_stops_whole_migration(self):
        letter = _join(
            _clean(self.letteros_lib, "Текстовые блоки", "Текст 16px"),
            _clean(self.letteros_lib, "Дополнительные Баннеры", "Вариант 7"),
            _clean(self.letteros_lib, "Мероприятия", "Расписание 2"),
        )
        with self.assertRaises(lm.MigrationError) as ctx:
            lm.migrate_letter(letter, self.schedule_path, letteros_library=self.letteros_lib)
        self.assertEqual(ctx.exception.module, "Дополнительные Баннеры")
        self.assertEqual(ctx.exception.element, "Вариант 7")
        self.assertIn("ручной проверки", str(ctx.exception))


class WrapperStrippedImageMigrationTests(unittest.TestCase):
    """Класс 1 (см. letteros_recognition.extract_wrapper_stripped_inner()) для
    image-slot компонентов, сквозь весь migrate_letter(): Letteros production-
    фрагмент "одиночная картинка в click-tracking ссылке" без внешней card-
    обвязки/MSO ghost-table (<td>-><table>?-><a>?-><img>, см. диагностику
    STOP-фрагмента) распознаётся как существующий canonical
    "Изображения/Изображение шириной 600px" и СОБИРАЕТСЯ заново в полный
    canonical UniSender-блок (letteros_adapt._rebuild_wrapper_stripped() +
    _transplant_image_slot(), существующий механизм image-slot --
    generation._fill_first_local_image()) -- не сохраняется как сырой HTML
    (в отличие от прежнего RAW_IMAGE_PASSTHROUGH, который этот тест заменяет)."""

    _TRACKING_HREF = (
        "https://api.letteros.com/pixel/177156/link/8a1df8de-99ed-4eb8-8c80-"
        "42536d75a5e2?url=aHR0cHM6Ly9lbmdpbmVlci1oaXN0b3J5LnJ1Lz91dG1fc291cmNl"
        "PWVtYWlsLXJlZ3VsYXItbXNr"
    )

    @classmethod
    def setUpClass(cls):
        cls.letteros_lib = lr.load_letteros_library()
        cls.tmpdir = tempfile.TemporaryDirectory()
        cls.schedule_path = _write_schedule_txt(Path(cls.tmpdir.name))

    @classmethod
    def tearDownClass(cls):
        cls.tmpdir.cleanup()

    def _raw_image_block(self, img_src="https://s3.letteros.com/user-uploads/x.jpg", width="600"):
        # Тот же паттерн, что и в реальных письмах: <td align="center"> без
        # bgcolor/padding/border-radius, ширина/border-radius отличаются от
        # canonical (536px + radius) -- production-геометрия картинки не
        # переносится (см. IMAGE_SLOTS.md §9-10), только src/href.
        return (
            f'<tr><td align="center" class=""><a href="{self._TRACKING_HREF}" '
            f'target="_blank" style="font-family: Arial, Tahoma, sans-serif; '
            f'font-size: 14px; color: #000000; text-decoration: none;" class=""> '
            f'<img src="{img_src}" width="{width}" alt="" border="0" '
            f'style="display: block; width: 100%; max-width: {width}px;" class=""></a></td></tr>'
        )

    def test_letter_with_raw_image_block_migrates_as_canonical_component(self):
        letter = _join(
            _clean(self.letteros_lib, "Текстовые блоки", "Текст 16px"),
            self._raw_image_block(),
            _clean(self.letteros_lib, "Мероприятия", "Расписание 1"),
        )
        result = lm.migrate_letter(letter, self.schedule_path, letteros_library=self.letteros_lib)
        self.assertIn(("Изображения", "Изображение шириной 600px"), result.components)

    def test_letteros_tracking_href_removed_but_image_src_kept(self):
        letter = _join(
            _clean(self.letteros_lib, "Текстовые блоки", "Текст 16px"),
            self._raw_image_block(img_src="https://s3.letteros.com/user-uploads/keep-me.jpg"),
            _clean(self.letteros_lib, "Мероприятия", "Расписание 1"),
        )
        result = lm.migrate_letter(letter, self.schedule_path, letteros_library=self.letteros_lib)
        self.assertNotIn("api.letteros.com/pixel", result.html)
        self.assertIn('src="https://s3.letteros.com/user-uploads/keep-me.jpg"', result.html)

    def test_canonical_wrapper_and_geometry_are_rebuilt_not_preserved_from_production(self):
        # Ключевое отличие от прежнего raw passthrough: card-обвязка/MSO
        # ghost-table/геометрия картинки (536px, border-radius) -- из
        # canonical UniSender-компонента, а НЕ из production (там 600px, без
        # border-radius -- см. _raw_image_block()).
        letter = _join(
            _clean(self.letteros_lib, "Текстовые блоки", "Текст 16px"),
            self._raw_image_block(),
            _clean(self.letteros_lib, "Мероприятия", "Расписание 1"),
        )
        result = lm.migrate_letter(letter, self.schedule_path, letteros_library=self.letteros_lib)
        self.assertIn("<!--[if (gte mso 9)|(IE)]>", result.html)  # MSO ghost-table восстановлен
        self.assertIn('max-width: 536px; border-radius: 8px;', result.html)  # canonical-геометрия
        self.assertNotIn('max-width: 600px', result.html)  # production-ширина не перенесена
        self.assertIn('em="block" class="em-structure"', result.html)

    def test_other_unresolved_content_still_stops_migration(self):
        # Class 1 -- узкое исключение для одной конкретной структурной формы,
        # а не общее "пропускать всё непонятное".
        letter = _join(
            _clean(self.letteros_lib, "Текстовые блоки", "Текст 16px"),
            self._raw_image_block(),
            "<p>совершенно случайный HTML не из библиотеки компонентов</p>",
            _clean(self.letteros_lib, "Мероприятия", "Расписание 1"),
        )
        with self.assertRaises(lm.MigrationError):
            lm.migrate_letter(letter, self.schedule_path, letteros_library=self.letteros_lib)

    def test_nested_duplicate_candidate_not_absorbed_twice(self):
        # Имитация реального паттерна (Казань/Мск/ГИ special, см.
        # диагностику): внешний <table>-враппер и вложенный в него
        # <tr><td><a><img></a></td></tr> оба по отдельности структурно похожи
        # на wrapper_stripped-кандидата, но это один и тот же <img> -- в
        # сборке должен остаться один блок, не два (см.
        # letteros_recognition.recognize_components(), финальная фильтрация
        # unresolved, содержащих подтверждённый match).
        inner = self._raw_image_block()
        outer = (
            '<tr><td align="center"> <table width="100%" border="0" '
            f'cellspacing="0" cellpadding="0">{inner}</table> </td></tr>'
        )
        letter = _join(
            _clean(self.letteros_lib, "Текстовые блоки", "Текст 16px"),
            outer,
            _clean(self.letteros_lib, "Мероприятия", "Расписание 1"),
        )
        result = lm.migrate_letter(letter, self.schedule_path, letteros_library=self.letteros_lib)
        image_count = sum(1 for module, element in result.components if (module, element) == ("Изображения", "Изображение шириной 600px"))
        self.assertEqual(image_count, 1)
        self.assertEqual(result.html.count('src="https://s3.letteros.com/user-uploads/x.jpg"'), 1)

    def test_real_fixtures_image_blocks_recognized_as_canonical_component(self):
        # Регрессия на 4 real fixtures: сколько картиночных production-
        # фрагментов без обвязки recognize_components() теперь распознаёт как
        # "Изображения/Изображение шириной 600px" (с учётом дедупликации
        # вложенных пар) -- fixture-specific baseline, см. комментарий у
        # RealProductionLettersTests.test_confirmed_counts_regression_baseline
        # в test_letteros_recognition.py (не production-правило).
        if not REAL_LETTERS_DIR.is_dir():
            raise unittest.SkipTest(f"каталог с реальными письмами не найден: {REAL_LETTERS_DIR}")
        expected_image_matches = {
            "ГИ special рассылка 21.04.2026.html": 1,
            "Казань Рассылка 30.07.2026.html": 1,
            "Копия — Мск 27.11.2025 рассылка.html": 1,
            "Рассылка 17.09.2026.html": 1,
            "Франшиза — экскурсия.html": 3,
        }
        for fp in sorted(REAL_LETTERS_DIR.glob("*.html")):
            raw = fp.read_text(encoding="utf-8")
            result = lr.recognize_components(raw, self.letteros_lib)
            image_matches = [
                m for m in result.matches
                if (m.module, m.element) == ("Изображения", "Изображение шириной 600px")
                and m.match_mode == "wrapper_stripped"
            ]
            self.assertEqual(len(image_matches), expected_image_matches[fp.name], fp.name)


class ScheduleModuleLevelMigrationTests(unittest.TestCase):
    """Класс 2: "Мероприятия" распознаётся на уровне модуля (element=None) по
    устойчивой day-шапке (день недели + дата), даже когда конкретный
    canonical "Расписание N" byte-в-byte не подтверждается -- в т.ч. когда (как
    в реальных Казань/Мск, см. диагностику) у production-фрагмента нет внешней
    card-обвязки. Element сознательно не выбирается -- он и не нужен: старый
    блок расписания при миграции отбрасывается целиком независимо от того,
    какой конкретно Расписание N это был (см. _split_schedule_and_determine_
    insertion() в letteros_migrate.py, не изменялась)."""

    @classmethod
    def setUpClass(cls):
        cls.letteros_lib = lr.load_letteros_library()
        cls.tmpdir = tempfile.TemporaryDirectory()
        cls.schedule_path = _write_schedule_txt(Path(cls.tmpdir.name))

    @classmethod
    def tearDownClass(cls):
        cls.tmpdir.cleanup()

    def _stripped_schedule_card(self):
        # Тот же паттерн, что и в Казань/Мск (см. диагностику): у production
        # блока расписания отсутствует внешняя card-обвязка -- остаётся только
        # "бордюрная" внутренняя <tr> (день/дата + события). letteros-*
        # снимаются ДО извлечения -- как и в реальном письме (см.
        # _strip_letteros_attrs()/_clean()); иначе длинные letteros-* атрибуты
        # самой library (в т.ч. кириллический letteros-tag) раздувают <td> за
        # пределы 200-символьного окна поиска "<td> сразу после <tr>" внутри
        # recognize_components() -- реальной production-проблемы это не
        # касается (там этих атрибутов нет вовсе), только сборки фикстуры.
        canon = _strip_letteros_attrs(self.letteros_lib[("Мероприятия", "Расписание 2")].html)
        stripped = lr.extract_wrapper_stripped_inner(canon)
        self.assertIsNotNone(stripped)
        return stripped

    def test_schedule_without_outer_wrapper_recognized_as_module_level_and_dropped(self):
        # Без отдельного подвала -- старый блок расписания сам задаёт точку
        # вставки нового (см. _split_schedule_and_determine_insertion()); не
        # используем "Подвалы" здесь намеренно: module-level "Подвалы" ВСЕГДА
        # REQUIRES_MANUAL_REVIEW независимо от конкретного element (см.
        # ModuleLevelAdaptationTests в test_letteros_adapt.py) -- это отдельное,
        # не связанное с этим тестом поведение, которое иначе маскировало бы
        # именно то, что здесь проверяется.
        letter = _join(
            _clean(self.letteros_lib, "Текстовые блоки", "Текст 16px"),
            self._stripped_schedule_card(),
        )
        result = lm.migrate_letter(letter, self.schedule_path, letteros_library=self.letteros_lib)
        for module, _element in result.components:
            self.assertNotEqual(module, "Мероприятия")

    def test_old_schedule_sample_content_replaced_by_new_schedule(self):
        stripped = self._stripped_schedule_card()
        day_sample = re.search(r'color: #DD776F[^>]*>\s*([^<]+?)\s*<', stripped).group(1)
        letter = _join(
            _clean(self.letteros_lib, "Текстовые блоки", "Текст 16px"),
            stripped,
        )
        result = lm.migrate_letter(letter, self.schedule_path, letteros_library=self.letteros_lib)
        self.assertNotIn(day_sample, result.html)  # старый образец даты не перенесён
        self.assertIn("Тестовая лекция миграции", result.html)  # новое расписание вставлено


class HeadingTextInjectionMigrationTests(unittest.TestCase):
    """Класс 3: заголовок "Текстовые блоки/Заголовок NNpx" распознаётся по
    font-size, даже когда production оформил его вручную иначе (align,
    <strong> вместо CSS font-weight, лишний class от другого варианта, другой
    line-height/letter-spacing -- см. диагностику Мск/ГИ special). Переносится
    только извлечённый чистый текст в canonical UniSender-структуру -- мусор
    (<strong>/<span>/лишний class/line-height/letter-spacing) не переносится."""

    @classmethod
    def setUpClass(cls):
        cls.letteros_lib = lr.load_letteros_library()
        cls.tmpdir = tempfile.TemporaryDirectory()
        cls.schedule_path = _write_schedule_txt(Path(cls.tmpdir.name))

    @classmethod
    def tearDownClass(cls):
        cls.tmpdir.cleanup()

    def _drifted_heading(self, text="Новый раздел — лекции"):
        # Тот же паттерн, что и в реальных письмах (Мск/ГИ special): align
        # центрирован вместо left, текст в <span>/<strong> вместо font-weight
        # в style, лишний class="mob_fs_48" (валиден только для 48px), лишний
        # letter-spacing, другой line-height -- font-size (34px) единственный
        # устойчивый признак, по которому распознаётся "Заголовок 34px".
        return (
            '<tr><td align="center" bgcolor="#FFFFFF" style="background-color: #FFFFFF; padding: 6px 15px;">'
            '<!--[if (gte mso 9)|(IE)]>'
            '<table width="536" border="0" cellspacing="0" cellpadding="0" style="width: 536px;">'
            '<tr><td>'
            '<![endif]-->'
            '<table border="0" cellpadding="0" cellspacing="0" style="max-width: 536px;" width="100%">'
            '<tr><td class="mob_fs_48" align="center" style="letter-spacing: -0.48px; font-size: 34px; '
            'font-family: Arial, Tahoma, sans-serif; line-height: 48px; color: #000000;">'
            f'<span style="font-size: 42px;"><strong>{text}</strong></span>'
            '</td></tr>'
            '</table>'
            '<!--[if (gte mso 9)|(IE)]>'
            '</td></tr>'
            '</table>'
            '<![endif]-->'
            '</td></tr>'
        )

    def test_letter_with_drifted_heading_migrates_as_canonical_heading(self):
        letter = _join(
            self._drifted_heading(),
            _clean(self.letteros_lib, "Мероприятия", "Расписание 1"),
        )
        result = lm.migrate_letter(letter, self.schedule_path, letteros_library=self.letteros_lib)
        self.assertIn(("Текстовые блоки", "Заголовок 34px"), result.components)

    def test_clean_text_preserved_but_strong_span_and_drift_not(self):
        letter = _join(
            self._drifted_heading(text="Куда сходить в Москве"),
            _clean(self.letteros_lib, "Мероприятия", "Расписание 1"),
        )
        result = lm.migrate_letter(letter, self.schedule_path, letteros_library=self.letteros_lib)
        self.assertIn("Куда сходить в Москве", result.html)
        # <strong>/<span style="font-size: 42px"> вокруг заголовка не
        # перенесены (у самого блока расписания, использованного как филлер в
        # этом письме, есть свой легитимный "<span><span>подробнее</span>..."
        # -- поэтому проверяем не глобальное отсутствие "<span", а именно
        # отсутствие production-мусора заголовка).
        self.assertNotIn(f"<strong>Куда сходить в Москве</strong>", result.html)
        self.assertNotIn('<span style="font-size: 42px;">', result.html)
        # "mob_fs_48" сам по себе легитимно встречается в стандартном CSS
        # документа (media query для 48px-заголовков) -- проверяем именно
        # отсутствие production class="mob_fs_48" НА самом теге заголовка.
        self.assertNotIn('class="mob_fs_48"', result.html)
        self.assertNotIn("letter-spacing", result.html)
        self.assertNotIn("line-height: 48px", result.html)  # canonical "Заголовок 34px" -- line-height: 37px

    def test_canonical_structure_restored(self):
        letter = _join(
            self._drifted_heading(),
            _clean(self.letteros_lib, "Мероприятия", "Расписание 1"),
        )
        result = lm.migrate_letter(letter, self.schedule_path, letteros_library=self.letteros_lib)
        # canonical "Заголовок 34px" -- align="left" (не production-центр) и
        # жирность через CSS font-weight (не production-<strong>) -- обвязка
        # СОБРАНА заново из canonical, а не унаследована от production-drift.
        self.assertIn('align="left"', result.html)
        self.assertIn("font-weight: 700", result.html)
        self.assertIn("line-height: 37px", result.html)


class GridContentInjectionMigrationTests(unittest.TestCase):
    """Класс 4: 2-колоночный grid "Контентные блоки/Вариант 2-4" -- content-
    aware распознавание accent card на каждом item'е (белый/teal/коралл), 0/1/2
    tag-pill на item (production не гарантирует ровно 2, как canonical -- см.
    диагностику), перенос картинки/заголовка/текста/CTA, удаление Letteros
    tracking href, системная CTA-иконка берётся только из canonical (не
    production). Фикстуры собраны из реальных canonical Letteros-компонентов
    (letteros_lib) через letteros_recognition.split_grid_items() -- то же
    разбиение, которым пользуется и production-код, поэтому гарантированно
    совместимо с extract_grid_items()/_GRID_TAG_PILL_RE."""

    _TRACKING_HREF = (
        "https://api.letteros.com/pixel/999999/link/aaaaaaaa-99ed-4eb8-8c80-"
        "42536d75a5e2?url=aHR0cHM6Ly9lbmdpbmVlci1oaXN0b3J5LnJ1Lz91dG1fc291cmNl"
    )

    @classmethod
    def setUpClass(cls):
        cls.letteros_lib = lr.load_letteros_library()
        cls.tmpdir = tempfile.TemporaryDirectory()
        cls.schedule_path = _write_schedule_txt(Path(cls.tmpdir.name))

    @classmethod
    def tearDownClass(cls):
        cls.tmpdir.cleanup()

    def _swap_items(self, html: str) -> str:
        prefix, item1, connector, item2, suffix = lr.split_grid_items(html)
        return (
            prefix + lr.GRID_ITEM_OPEN_MARKER + item2 + lr.GRID_ITEM_CLOSE_MARKER
            + connector + lr.GRID_ITEM_OPEN_MARKER + item1 + lr.GRID_ITEM_CLOSE_MARKER + suffix
        )

    def _remove_tag_pill(self, item_html: str, index: int) -> str:
        m = list(lr._GRID_TAG_PILL_RE.finditer(item_html))[index]
        td_start = item_html.rfind("<td", 0, m.start())
        td_end = generation._scan_balanced_td_end(item_html, td_start)
        return item_html[:td_start] + item_html[td_end:]

    def _strip_item_tags(self, html: str, item_index: int, keep: int) -> str:
        prefix, item1, connector, item2, suffix = lr.split_grid_items(html)
        items = [item1, item2]
        target = items[item_index]
        if keep == 0:
            target = self._remove_tag_pill(target, 0)
            target = self._remove_tag_pill(target, 0)
        elif keep == 1:
            target = self._remove_tag_pill(target, 1)
        items[item_index] = target
        return (
            prefix + lr.GRID_ITEM_OPEN_MARKER + items[0] + lr.GRID_ITEM_CLOSE_MARKER
            + connector + lr.GRID_ITEM_OPEN_MARKER + items[1] + lr.GRID_ITEM_CLOSE_MARKER + suffix
        )

    def _inject_tracking_href(self, html: str) -> str:
        return html.replace('href="#"', f'href="{self._TRACKING_HREF}"')

    def _grid_html(self, result_html: str) -> str:
        start = result_html.index("<!-- items -->")
        end = result_html.index("<!-- items END -->")
        return result_html[start:end]

    def test_two_tag_pill_accent_on_left_migrates_via_grid_content_injection(self):
        # Вариант 4 canonical: слева белый, справа teal (#479F98) -- меняем
        # местами, получая accent СЛЕВА -- не совпадает целиком ни с одним
        # существующим (module, element), поэтому идёт через класс 4, а не
        # через обычный full-match.
        base = _clean(self.letteros_lib, "Контентные блоки", "Вариант 4")
        swapped = self._inject_tracking_href(self._swap_items(base))
        swapped = swapped.replace(
            "images/8fa463f2-1a24-42ae-b4ca-ed6af2b46171.jpg", "https://s3.letteros.com/user-uploads/left.jpg",
        ).replace(
            "images/0d613332-0c2a-4722-b102-84ade2f27546.jpg", "https://s3.letteros.com/user-uploads/right.jpg",
        )
        letter = _join(
            _clean(self.letteros_lib, "Текстовые блоки", "Текст 16px"),
            swapped,
            _clean(self.letteros_lib, "Мероприятия", "Расписание 1"),
        )
        result = lm.migrate_letter(letter, self.schedule_path, letteros_library=self.letteros_lib)
        self.assertIn((lr.GRID_MODULE_NAME, None), result.components)

        grid_html = self._grid_html(result.html)
        # accent (teal) идёт первым -- слева
        self.assertLess(grid_html.index('bgcolor="#479F98"'), grid_html.index('bgcolor="#FFFFFF"'))
        # оба tag-pild'а на каждом item'е сохранены (production здесь их не убирал)
        self.assertEqual(grid_html.count('letteros-hide="ярлык'), 4)
        # image-slot: production src перенесён (не canonical placeholder)
        self.assertIn('src="https://s3.letteros.com/user-uploads/left.jpg"', result.html)
        self.assertIn('src="https://s3.letteros.com/user-uploads/right.jpg"', result.html)
        # системная CTA-иконка -- canonical asset (img.hiteml.com), не production
        self.assertIn("img.hiteml.com", grid_html)
        # Letteros tracking href нигде не остался
        self.assertNotIn("api.letteros.com/pixel", result.html)
        # редактируемая UniSender-структура (em=block) сохранена
        self.assertIn('em="block" class="em-structure"', result.html)

    def test_zero_tag_pill_both_white_items_migrates(self):
        # Вариант 2 canonical: оба item'а белые -- убираем tag-pild'ы у ОБОИХ
        # (0 вместо 2, как у ГИ special в реальном письме).
        base = _clean(self.letteros_lib, "Контентные блоки", "Вариант 2")
        stripped = self._strip_item_tags(base, 0, keep=0)
        stripped = self._strip_item_tags(stripped, 1, keep=0)
        stripped = self._inject_tracking_href(stripped)
        letter = _join(
            _clean(self.letteros_lib, "Текстовые блоки", "Текст 16px"),
            stripped,
            _clean(self.letteros_lib, "Мероприятия", "Расписание 1"),
        )
        result = lm.migrate_letter(letter, self.schedule_path, letteros_library=self.letteros_lib)
        self.assertIn((lr.GRID_MODULE_NAME, None), result.components)

        grid_html = self._grid_html(result.html)
        self.assertEqual(grid_html.count('bgcolor="#FFFFFF"'), 2)  # оба item'а белые
        # tag-pild'ы отсутствуют у ОБОИХ item'ов -- родная опциональность
        # компонента (letteros-hide), текст не выдуман
        self.assertNotIn('letteros-hide="все ярлыки"', grid_html)
        self.assertNotIn('letteros-hide="ярлык', grid_html)
        self.assertNotIn("api.letteros.com/pixel", result.html)
        self.assertIn('em="block" class="em-structure"', result.html)

    def test_one_tag_pill_per_item_migrates(self):
        # Вариант 3 canonical (белый/коралл) -- оставляем по 1 tag-pild'у на
        # item (как у Мск в реальном письме), второй удалён -- первый сохранён.
        base = _clean(self.letteros_lib, "Контентные блоки", "Вариант 3")
        stripped = self._strip_item_tags(base, 0, keep=1)
        stripped = self._strip_item_tags(stripped, 1, keep=1)
        stripped = self._inject_tracking_href(stripped)
        letter = _join(
            _clean(self.letteros_lib, "Текстовые блоки", "Текст 16px"),
            stripped,
            _clean(self.letteros_lib, "Мероприятия", "Расписание 1"),
        )
        result = lm.migrate_letter(letter, self.schedule_path, letteros_library=self.letteros_lib)
        self.assertIn((lr.GRID_MODULE_NAME, None), result.components)

        grid_html = self._grid_html(result.html)
        self.assertEqual(grid_html.count('letteros-hide="ярлык'), 2)  # ровно по 1 на item
        # строка-обёртка "все ярлыки" остаётся (внутри неё 1 pill), в отличие
        # от 0-tag случая, где убирается целиком
        self.assertEqual(grid_html.count('letteros-hide="все ярлыки"'), 2)
        self.assertNotIn("api.letteros.com/pixel", result.html)
        self.assertIn('em="block" class="em-structure"', result.html)

    def test_other_unresolved_content_still_stops_migration(self):
        # Класс 4 -- узкое исключение для конкретной структурной формы, а не
        # общее "пропускать всё непонятное".
        base = _clean(self.letteros_lib, "Контентные блоки", "Вариант 2")
        stripped = self._inject_tracking_href(self._strip_item_tags(base, 0, keep=0))
        letter = _join(
            _clean(self.letteros_lib, "Текстовые блоки", "Текст 16px"),
            stripped,
            "<p>совершенно случайный HTML не из библиотеки компонентов</p>",
            _clean(self.letteros_lib, "Мероприятия", "Расписание 1"),
        )
        with self.assertRaises(lm.MigrationError):
            lm.migrate_letter(letter, self.schedule_path, letteros_library=self.letteros_lib)


class ScheduleInsertionPointTests(unittest.TestCase):
    """_split_schedule_and_determine_insertion() напрямую -- см. модульный
    docstring этого файла о том, почему не через полный migrate_letter() с
    настоящим подвалом в HTML."""

    def test_insertion_before_footer_when_no_old_schedule(self):
        matches = [
            _fake_match("Текстовые блоки", "Текст 16px", 0),
            _fake_match("Кнопки", "Зелёная кнопка на всю ширину", 1),
            _fake_match("Подвалы", "Москва 1", 2),
        ]
        non_schedule, idx = lm._split_schedule_and_determine_insertion(matches)
        self.assertEqual(
            [(m.module, m.element) for m in non_schedule],
            [("Текстовые блоки", "Текст 16px"), ("Кнопки", "Зелёная кнопка на всю ширину"), ("Подвалы", "Москва 1")],
        )
        self.assertEqual(idx, 2)  # прямо перед подвалом

    def test_insertion_at_old_schedule_position_when_present(self):
        matches = [
            _fake_match("Текстовые блоки", "Текст 16px", 0),
            _fake_match("Мероприятия", "Расписание 1", 1),
            _fake_match("Подвалы", "Москва 1", 2),
        ]
        non_schedule, idx = lm._split_schedule_and_determine_insertion(matches)
        self.assertEqual(
            [(m.module, m.element) for m in non_schedule],
            [("Текстовые блоки", "Текст 16px"), ("Подвалы", "Москва 1")],
        )
        self.assertEqual(idx, 1)

    def test_multiple_old_schedule_blocks_all_removed_insertion_at_first(self):
        matches = [
            _fake_match("Текстовые блоки", "Текст 16px", 0),
            _fake_match("Мероприятия", "Расписание 1", 1),
            _fake_match("Мероприятия", "Расписание 2", 2),
            _fake_match("Подвалы", "Москва 1", 3),
        ]
        non_schedule, idx = lm._split_schedule_and_determine_insertion(matches)
        self.assertEqual(
            [(m.module, m.element) for m in non_schedule],
            [("Текстовые блоки", "Текст 16px"), ("Подвалы", "Москва 1")],
        )
        self.assertEqual(idx, 1)

    def test_no_schedule_and_no_footer_stops(self):
        matches = [
            _fake_match("Текстовые блоки", "Текст 16px", 0),
            _fake_match("Кнопки", "Зелёная кнопка на всю ширину", 1),
        ]
        with self.assertRaises(lm.MigrationError) as ctx:
            lm._split_schedule_and_determine_insertion(matches)
        self.assertIn("точку вставки", str(ctx.exception))


class ModuleLevelHeaderFooterMigrationTests(unittest.TestCase):
    """Module-level recognition (см. модульный docstring) для "Шапки"/
    "Подвалы" в контексте полного migrate_letter()."""

    @classmethod
    def setUpClass(cls):
        cls.letteros_lib = lr.load_letteros_library()
        cls.tmpdir = tempfile.TemporaryDirectory()
        cls.schedule_path = _write_schedule_txt(Path(cls.tmpdir.name))

    @classmethod
    def tearDownClass(cls):
        cls.tmpdir.cleanup()

    def test_letter_with_real_header_now_migrates_successfully(self):
        """Новая возможность этого изменения: у "Шапки" нет ни одной записи
        в KNOWN_ADAPTATION_EXCEPTIONS, поэтому письмо с настоящей шапкой (но
        без подвала -- см. следующий тест) теперь может дойти до успешной
        сборки целиком, используя фактический HTML шапки как есть."""
        header_html = self.letteros_lib[("Шапки", "Туроператор 2")].html
        letter = _join(
            self._strip(header_html),
            self._strip(self.letteros_lib[("Мероприятия", "Расписание 1")].html),
            self._strip(self.letteros_lib[("Кнопки", "Зелёная кнопка на всю ширину")].html),
        )
        result = lm.migrate_letter(letter, self.schedule_path, letteros_library=self.letteros_lib)
        self.assertEqual(result.components[0], ("Шапки", None))
        # реальный фрагмент шапки (после технической адаптации: em/class
        # добавлены, letteros-* не осталось) присутствует в результате как
        # есть -- его содержимое (тег <a> с исходной ссылкой на филиал) не
        # заменено каноническим вариантом
        header_href = re.search(r'href="([^"]*)"', header_html).group(1)
        self.assertIn(f'href="{header_href}"', result.html)

    def _strip(self, html):
        return _strip_letteros_attrs(html)

    def test_letter_with_real_footer_still_stops_but_for_a_safer_reason(self):
        """Подвал теперь распознаётся (не "не удалось однозначно распознать"),
        но всё ещё блокирует миграцию -- по конкретной, узкой причине:
        letteros_adapt.KNOWN_ADAPTATION_EXCEPTIONS содержит ("Подвалы",
        "Школа 1"), и без element нельзя ни исключить, ни подтвердить, что
        это она (см. letteros_adapt.adapt_component())."""
        letter = _join(
            self._strip(self.letteros_lib[("Текстовые блоки", "Текст 16px")].html),
            self._strip(self.letteros_lib[("Подвалы", "Москва 1")].html),
        )
        with self.assertRaises(lm.MigrationError) as ctx:
            lm.migrate_letter(letter, self.schedule_path, letteros_library=self.letteros_lib)
        message = str(ctx.exception)
        self.assertNotIn("однозначно распознать", message)  # это больше не проблема recognition
        self.assertIn("ручной проверки", message)
        self.assertEqual(ctx.exception.module, "Подвалы")
        self.assertIsNone(ctx.exception.element)


if __name__ == "__main__":
    unittest.main(verbosity=2)
