"""Тесты primitive'а Letteros -> UniSender, этап 1.5 (letteros_adapt.py):
техническая адаптация оболочки уже распознанного canonical Letteros-
компонента до формы, пригодной для UniSender, без извлечения/переноса
содержимого. Без сети, без изменения файлов проекта. Запуск:
python mail_project/test_letteros_adapt.py (или из корня репозитория).
"""

import re
import unittest
import zipfile
from pathlib import Path

import letteros_adapt as la
import letteros_recognition as lr

BASE_DIR = Path(__file__).resolve().parent
UNISENDER_ZIP = BASE_DIR / "unisender_components.zip"


def _load_unisender_library() -> dict:
    lib = {}
    with zipfile.ZipFile(UNISENDER_ZIP) as z:
        for name in z.namelist():
            if not name.endswith(".html"):
                continue
            module, _, rest = name.partition("/")
            element = rest[:-len(".html")]
            lib[(module, element)] = z.read(name).decode("utf-8")
    return lib


class AdaptAllComponentsTests(unittest.TestCase):
    """Прогон adapt_wrapper()/adapt_component() по всем 121 canonical
    Letteros-компонентам и сравнение результата с canonical UniSender-
    компонентом того же имени через letteros_recognition.structural_match() —
    воспроизводит эксперимент "121 пара" как регрессионный тест."""

    @classmethod
    def setUpClass(cls):
        cls.letteros_lib = lr.load_letteros_library()
        cls.unisender_lib = _load_unisender_library()
        cls.keys = sorted(cls.letteros_lib)
        cls.results = {}
        for key in cls.keys:
            module, element = key
            adapted = la.adapt_component(module, element, cls.letteros_lib[key].html)
            ok, diffs = lr.structural_match(cls.unisender_lib[key], adapted.html)
            cls.results[key] = (adapted, ok, diffs)

    def test_all_121_components_present_in_both_libraries(self):
        self.assertEqual(len(self.keys), 121)
        self.assertEqual(set(self.keys), set(self.unisender_lib))

    def test_118_components_are_structurally_unisender_compatible(self):
        compatible = {k for k, (adapted, ok, diffs) in self.results.items() if ok}
        self.assertEqual(len(compatible), 118, sorted(set(self.keys) - compatible))

    def test_incompatible_components_are_exactly_the_known_three(self):
        incompatible = {k for k, (adapted, ok, diffs) in self.results.items() if not ok}
        self.assertEqual(incompatible, set(la.KNOWN_ADAPTATION_EXCEPTIONS))

    def test_known_exceptions_flagged_requires_manual_review(self):
        for key in la.KNOWN_ADAPTATION_EXCEPTIONS:
            adapted, ok, diffs = self.results[key]
            self.assertEqual(adapted.status, la.AdaptationStatus.REQUIRES_MANUAL_REVIEW, key)
            self.assertIsNotNone(adapted.note)
            self.assertFalse(ok, f"{key}: числится REQUIRES_MANUAL_REVIEW, но структурная проверка внезапно прошла")

    def test_supported_components_are_not_the_known_exceptions(self):
        for key, (adapted, ok, diffs) in self.results.items():
            if key in la.KNOWN_ADAPTATION_EXCEPTIONS:
                continue
            self.assertEqual(adapted.status, la.AdaptationStatus.SUPPORTED, key)
            self.assertIsNone(adapted.note, key)
            self.assertTrue(ok, f"{key}: числится SUPPORTED, но структурная проверка не проходит: {diffs[:3]}")

    def test_editor_only_attrs_removed_everywhere(self):
        seen_before = {attr: 0 for attr in la.EDITOR_ONLY_ATTRS_TO_STRIP}
        for key in self.keys:
            raw = self.letteros_lib[key].html
            adapted, _ok, _diffs = self.results[key]
            for attr in la.EDITOR_ONLY_ATTRS_TO_STRIP:
                if attr + "=" in raw:
                    seen_before[attr] += 1
                self.assertNotIn(attr + "=", adapted.html, f"{key}: {attr} остался после adapt_wrapper()")
        for attr, count in seen_before.items():
            self.assertGreater(count, 0, f"{attr} ни разу не встретился в исходной библиотеке — тест ничего не проверяет")

    def test_preserved_letteros_attrs_survive_everywhere(self):
        # Значение letteros-element сверяем с тем, что реально было в
        # исходнике, а не с именем файла в zip — для 3 файлов модуля
        # "Текстовые блоки" они расходятся из-за запятой в названии (известный,
        # не связанный с этим изменением факт — см. project memory
        # project-email-generation-v1: то же несовпадение уже фиксировалось
        # для manifest.json).
        for key in self.keys:
            module, element = key
            raw = self.letteros_lib[key].html
            adapted, _ok, _diffs = self.results[key]
            m = re.search(r'letteros-element="([^"]*)"', raw)
            self.assertIsNotNone(m, key)
            self.assertIn(f'letteros-element="{m.group(1)}"', adapted.html)
            self.assertIn(f'letteros-module="{module}"', adapted.html)
            for attr in ("letteros-hide", "letteros-no-utm"):
                self.assertEqual(
                    raw.count(attr + "="), adapted.html.count(attr + "="),
                    f"{key}: количество {attr} изменилось",
                )

    def test_wrapper_marker_added_everywhere(self):
        for key in self.keys:
            adapted, _ok, _diffs = self.results[key]
            self.assertTrue(
                adapted.html.startswith('<tr em="block" class="em-structure"'),
                f"{key}: не найден ожидаемый маркер в начале адаптированного HTML",
            )


class ContentPreservationTests(unittest.TestCase):
    """adapt_wrapper() не должен трогать текст/ссылки/картинки ни в одном из
    известных каналов изображения-как-контента."""

    @classmethod
    def setUpClass(cls):
        cls.lib = lr.load_letteros_library()

    def test_text_unchanged(self):
        html = self.lib[("Мероприятия", "Расписание 1")].html
        adapted = la.adapt_wrapper(html)
        for sample in ("пятница", "Дом переехал", "ЗАПИСАТЬСЯ", "19\xa0СЕНТЯБРЯ" if "19\xa0СЕНТЯБРЯ" in html else "СЕНТЯБРЯ"):
            self.assertIn(sample, html)
            self.assertIn(sample, adapted)

    def test_href_unchanged(self):
        html = self.lib[("Шапки", "Москва 1")].html
        adapted = la.adapt_wrapper(html)
        before = re.findall(r'href="([^"]*)"', html)
        after = re.findall(r'href="([^"]*)"', adapted)
        self.assertTrue(before)
        self.assertEqual(before, after)

    def test_img_src_unchanged(self):
        html = self.lib[("Дополнительные Баннеры", "Вариант 1")].html
        adapted = la.adapt_wrapper(html)
        before = re.findall(r'<img[^>]*\bsrc="([^"]*)"', html)
        after = re.findall(r'<img[^>]*\bsrc="([^"]*)"', adapted)
        self.assertTrue(before)
        self.assertEqual(before, after)

    def test_background_image_channels_unchanged(self):
        """<td background>, CSS background-image и VML <v:image src> — три
        канала картинки-как-контента (см. letteros_recognition.py) —
        физически не меняются adapt_wrapper() ни на йоту, только считаются
        "переменными" при сравнении структуры, а не при самой адаптации."""
        html = self.lib[("Баннеры", "Вариант 1")].html
        adapted = la.adapt_wrapper(html)
        patterns = (
            r'(?<!-)\bbackground="[^"]*"',  # (?<!-): не letteros-background="..."
            r'background-image:\s*url\([^)]*\)',
            r'<v:image\b[^>]*\bsrc="[^"]*"',
        )
        for pattern in patterns:
            before = re.findall(pattern, html)
            after = re.findall(pattern, adapted)
            self.assertTrue(before, f"канал {pattern!r} не встретился в тестовом компоненте — тест ничего не проверяет")
            self.assertEqual(before, after, pattern)

    def test_css_and_nesting_unchanged_byte_for_byte_outside_wrapper_attrs(self):
        """Более сильная проверка: adapt_wrapper() не трогает НИЧЕГО, кроме
        конкретных 7 имён атрибутов и открывающего <tr>. Проверяем это прямым
        построчным дифом после отмены обеих трансформаций вручную."""
        html = self.lib[("Контентные блоки", "Вариант 1")].html
        adapted = la.adapt_wrapper(html)
        # откатываем добавление маркера
        self.assertTrue(adapted.startswith('<tr em="block" class="em-structure"'))
        rolled_back = "<tr" + adapted[len('<tr em="block" class="em-structure"'):]
        # то, что осталось после отката маркера, должно совпадать с исходником
        # с точностью до наличия/отсутствия ровно 7 editor-only атрибутов
        stripped_original = la._EDITOR_ONLY_ATTR_RE.sub("", html)
        self.assertEqual(rolled_back, stripped_original)


class ModuleLevelAdaptationTests(unittest.TestCase):
    """adapt_component() для module-level компонентов (element=None, см.
    letteros_recognition.RecognizedComponent) — "Шапки"/"Подвалы"."""

    @classmethod
    def setUpClass(cls):
        cls.lib = lr.load_letteros_library()

    def test_header_module_level_is_supported(self):
        # У "Шапки" в KNOWN_ADAPTATION_EXCEPTIONS нет ни одной записи -- риска
        # попасть в исключение нет, независимо от того, какой конкретно
        # element имел исходный фрагмент.
        html = self.lib[("Шапки", "Казань 2")].html
        adapted = la.adapt_component("Шапки", None, html)
        self.assertEqual(adapted.status, la.AdaptationStatus.SUPPORTED)
        self.assertIsNone(adapted.note)
        self.assertEqual(adapted.element, None)
        self.assertTrue(adapted.html.startswith('<tr em="block" class="em-structure"'))

    def test_footer_module_level_requires_manual_review(self):
        # "Подвалы" содержит известное исключение ("Подвалы", "Школа 1") --
        # без element нельзя ни подтвердить, ни исключить, что это именно
        # оно, поэтому module-level "Подвалы" ВСЕГДА REQUIRES_MANUAL_REVIEW,
        # а не молча SUPPORTED. Взят элемент, который сам по себе не
        # исключение ("Москва 1") -- показывает, что осторожность применяется
        # ко всему модулю целиком, а не угадывается по конкретному фрагменту.
        html = self.lib[("Подвалы", "Москва 1")].html
        adapted = la.adapt_component("Подвалы", None, html)
        self.assertEqual(adapted.status, la.AdaptationStatus.REQUIRES_MANUAL_REVIEW)
        self.assertIsNotNone(adapted.note)
        # причина явно НЕ звучит как утверждение "это точно Школа 1"
        self.assertNotIn("рассинхронизация состава", adapted.note)
        self.assertIn("Школа 1", adapted.note)  # но упоминает, какая именно запись создаёт риск

    def test_footer_module_level_html_still_technically_adapted(self):
        """REQUIRES_MANUAL_REVIEW не означает "адаптация не выполнена" --
        adapt_wrapper() всё равно применяется тем же образом, что и для
        SUPPORTED; статус только сообщает вызывающему коду не доверять
        результату молча (используется migrate_letter() для остановки)."""
        html = self.lib[("Подвалы", "Казань 3")].html
        adapted = la.adapt_component("Подвалы", None, html)
        self.assertTrue(adapted.html.startswith('<tr em="block" class="em-structure"'))
        for attr in la.EDITOR_ONLY_ATTRS_TO_STRIP:
            self.assertNotIn(attr + "=", adapted.html)
        self.assertIn('letteros-module="Подвалы"', adapted.html)

    def test_module_level_content_channels_unchanged(self):
        html = self.lib[("Шапки", "Москва 2")].html
        adapted = la.adapt_component("Шапки", None, html).html
        before = re.findall(r'<img[^>]*\bsrc="([^"]*)"', html)
        after = re.findall(r'<img[^>]*\bsrc="([^"]*)"', adapted)
        self.assertTrue(before)
        self.assertEqual(before, after)

    def test_module_outside_known_exceptions_is_never_forced_to_review_just_for_being_module_level(self):
        # Общая проверка модели: module-level сам по себе -- не причина для
        # REQUIRES_MANUAL_REVIEW, причина -- именно присутствие module в
        # KNOWN_ADAPTATION_EXCEPTIONS. Обе группы-исключения сегодня --
        # "Дополнительные Баннеры" и "Подвалы"; "Шапки" среди них нет.
        self.assertNotIn("Шапки", la._MODULES_WITH_KNOWN_EXCEPTIONS)
        self.assertIn("Подвалы", la._MODULES_WITH_KNOWN_EXCEPTIONS)


class LetterosTrackingHrefStrippedTests(unittest.TestCase):
    """Первая версия миграции (production-решение): href, указывающий на
    Letteros click-tracking редиректор (https://api.letteros.com/pixel/<id>/
    link/<uuid>?url=<base64>), удаляется целиком при adapt_wrapper() —
    исходный URL внутри НЕ декодируется и не восстанавливается (это сделает
    СММ позже, вручную, в UniSender). Удаляется именно href-атрибут, а не
    <a> целиком и не его содержимое — структура блока (картинка/текст/кнопка)
    не должна ломаться."""

    _TRACKING_HREF = (
        "https://api.letteros.com/pixel/177156/link/8a1df8de-99ed-4eb8-8c80-"
        "42536d75a5e2?url=aHR0cHM6Ly9lbmdpbmVlci1oaXN0b3J5LnJ1Lz91dG1fc291cmNl"
        "PWVtYWlsLXJlZ3VsYXItbXNr"
    )

    def test_letteros_tracking_href_is_removed(self):
        html = (
            f'<tr><td align="center"><a href="{self._TRACKING_HREF}" '
            'target="_blank"><img src="https://s3.letteros.com/x.jpg" '
            'width="536"></a></td></tr>'
        )
        adapted = la.adapt_wrapper(html)
        self.assertNotIn(self._TRACKING_HREF, adapted)
        self.assertNotIn("api.letteros.com/pixel", adapted)
        self.assertNotIn("href=", adapted)

    def test_a_content_and_other_attrs_survive_href_removal(self):
        html = (
            f'<tr><td align="center"><a href="{self._TRACKING_HREF}" '
            'target="_blank" style="color: #000000;"><img '
            'src="https://s3.letteros.com/x.jpg" width="536" alt="фото">'
            "текст ссылки</a></td></tr>"
        )
        adapted = la.adapt_wrapper(html)
        self.assertIn('<a target="_blank" style="color: #000000;">', adapted)
        self.assertIn('<img src="https://s3.letteros.com/x.jpg" width="536" alt="фото">', adapted)
        self.assertIn("текст ссылки</a>", adapted)

    def test_direct_schedule_href_is_not_removed(self):
        # Прямая ссылка билетов/расписания -- не Letteros tracking (нет
        # api.letteros.com/pixel/.../link/...), правило её не касается.
        html = (
            '<tr><td align="center"><a href="https://engineer-history.ru/'
            'events/12345" target="_blank">Расписание</a></td></tr>'
        )
        adapted = la.adapt_wrapper(html)
        self.assertIn('href="https://engineer-history.ru/events/12345"', adapted)

    def test_ordinary_non_letteros_href_untouched(self):
        html = (
            '<tr><td align="center"><a href="https://t.me/engineer_media" '
            'target="_blank">Telegram</a></td></tr>'
        )
        adapted = la.adapt_wrapper(html)
        self.assertIn('href="https://t.me/engineer_media"', adapted)

    def test_open_tracking_pixel_src_untouched(self):
        # Отдельный трекинг-пиксель (без "/link/") встречается в src, не в
        # href -- этим правилом не затрагивается вовсе.
        html = (
            '<tr><td><img id="_eoa_img" '
            'src="https://api.letteros.com/pixel/177156?v=abc"></td></tr>'
        )
        adapted = la.adapt_wrapper(html)
        self.assertIn('src="https://api.letteros.com/pixel/177156?v=abc"', adapted)

    def test_already_adapted_components_get_no_other_changes(self):
        # Ни один из 121 canonical Letteros-компонентов не содержит такой
        # href (проверено отдельно) -- значит для них adapt_wrapper() должен
        # вести себя ровно как раньше, без побочных эффектов нового правила.
        lib = lr.load_letteros_library()
        for key, entry in lib.items():
            self.assertNotIn("api.letteros.com/pixel", entry.html, key)
            adapted = la.adapt_wrapper(entry.html)
            self.assertNotIn("api.letteros.com/pixel", adapted, key)


class IdempotencyTests(unittest.TestCase):
    """Повторный вызов на уже адаптированном результате не должен ничего
    портить или менять повторно."""

    def test_second_call_is_noop_for_one_component(self):
        lib = lr.load_letteros_library()
        html = lib[("Мероприятия", "Расписание 1")].html
        once = la.adapt_wrapper(html)
        twice = la.adapt_wrapper(once)
        self.assertEqual(once, twice)

    def test_second_call_is_noop_across_whole_library(self):
        lib = lr.load_letteros_library()
        for key, entry in lib.items():
            once = la.adapt_wrapper(entry.html)
            twice = la.adapt_wrapper(once)
            self.assertEqual(once, twice, key)


def _print_status_summary():
    letteros_lib = lr.load_letteros_library()
    unisender_lib = _load_unisender_library()
    print("\n--- Статус адаптации всех 121 canonical Letteros-компонентов ---")
    supported, review = [], []
    for key in sorted(letteros_lib):
        module, element = key
        adapted = la.adapt_component(module, element, letteros_lib[key].html)
        ok, diffs = lr.structural_match(unisender_lib[key], adapted.html)
        (review if adapted.status == la.AdaptationStatus.REQUIRES_MANUAL_REVIEW else supported).append((key, ok, diffs))
    print(f"SUPPORTED: {len(supported)}")
    print(f"REQUIRES_MANUAL_REVIEW: {len(review)}")
    for key, ok, diffs in review:
        print(f"  {key[0]}/{key[1]}: structural_match={ok}")
        print(f"     note: {la.KNOWN_ADAPTATION_EXCEPTIONS[key]}")
        for d in diffs[:2]:
            print(f"     residual diff: {d}")
    bad_supported = [(k, diffs) for k, ok, diffs in supported if not ok]
    if bad_supported:
        print(f"!!! ВНИМАНИЕ: {len(bad_supported)} SUPPORTED-компонентов не проходят структурную проверку:")
        for k, diffs in bad_supported:
            print(f"   {k}: {diffs[:2]}")


if __name__ == "__main__":
    unittest.main(verbosity=2, exit=False)
    _print_status_summary()
