"""Тесты этапа 1 Letteros -> UniSender: только распознавание canonical
Letteros-компонентов в HTML (см. letteros_recognition.py). Без сети, без
изменения файлов проекта. Запуск: python mail_project/test_letteros_recognition.py
(или из корня репозитория — путь до этого файла).

Обновление: добавлено module-level recognition для "Шапки"/"Подвалы" (см.
letteros_recognition.MODULE_LEVEL_ELIGIBLE_MODULES) — эти два модуля
структурно неразличимы внутри себя на реальных данных (диагностика на 4
реальных Letteros-письмах проекта), поэтому вместо бесконечной
неоднозначности им разрешено распознаваться как (module, element=None).
Тесты, которые раньше фиксировали "остаётся unresolved" для этих двух
модулей, обновлены — это не регрессия, а последствие сознательного решения
не требовать больше несуществующего различения."""

import re
import unittest
from pathlib import Path

import letteros_recognition as lr

BASE_DIR = Path(__file__).resolve().parent
REAL_LETTER_PATH = BASE_DIR / "test" / "Копия — Копия — Футер Школа.html"
REAL_LETTERS_DIR = BASE_DIR / "letteros htmls"

_LETTEROS_ATTR_RE = re.compile(
    r'\s+(?:letteros-[a-z-]+|telegram-?)="[^"]*"', re.IGNORECASE
)


def _strip_letteros_attrs(html: str) -> str:
    """Тестовая утилита: имитирует то, что реально происходит при экспорте
    письма из Letteros (см. letteros_recognition.py, docstring модуля) — убирает
    все editor-only атрибуты, ничего больше не меняя. Не часть production-кода:
    нужна только чтобы честно проверить распознавание на "чистом" (без
    посторонних искажений конкретного тестового файла) инстансе компонента."""
    return _LETTEROS_ATTR_RE.sub("", html)


class LoadLibraryTests(unittest.TestCase):
    def test_library_has_121_entries(self):
        lib = lr.load_letteros_library()
        self.assertEqual(len(lib), 121)

    def test_known_component_present(self):
        lib = lr.load_letteros_library()
        self.assertIn(("Мероприятия", "Расписание 1"), lib)
        self.assertIn(("Шапки", "Школа 2"), lib)
        self.assertIn(("Подвалы", "Школа 1"), lib)


class SelfRecognitionOnCleanExportTests(unittest.TestCase):
    """Требование 1/2 из ТЗ: несколько canonical-компонентов непосредственно
    из letteros_components.zip, поданные как "чистый" экспорт (letteros-*
    атрибуты убраны, как это реально происходит при отправке письма — см.
    LEGACY_NEWSLETTER_MIGRATION.md и docstring letteros_recognition.py),
    должны быть распознаны корректно: правильные module/element, правильный
    порядок, правильные границы."""

    @classmethod
    def setUpClass(cls):
        cls.library = lr.load_letteros_library()

    def _clean(self, module, element):
        html = self.library[(module, element)].html
        cleaned = _strip_letteros_attrs(html)
        self.assertNotIn("letteros-element", cleaned)
        self.assertNotIn("letteros-module", cleaned)
        return cleaned

    def test_single_component_schedule(self):
        html = self._clean("Мероприятия", "Расписание 1")
        result = lr.recognize_components(html, self.library)
        self.assertEqual(len(result.matches), 1, result.unresolved)
        m = result.matches[0]
        self.assertEqual((m.module, m.element), ("Мероприятия", "Расписание 1"))
        self.assertEqual(m.order, 0)
        self.assertEqual((m.start, m.end), (0, len(html)))

    def test_header_family_resolves_as_module_level_component(self):
        """Честный, важный для этапа 1 факт: "Шапки/Москва 1" структурно
        ПОЛНОСТЬЮ идентична остальным 4 вариантам "...N 1" (Школа 1,
        Туроператор 1, Санкт-Петербург 1, Казань 1) — они отличаются только
        логотипом/ссылкой (src/href), а это "переменное" содержимое, которое
        этап 1 сознательно не сравнивает. Единственного подтверждённого
        canonical element тут никогда не будет — вместо бесконечной
        неоднозначности для этого модуля (см. MODULE_LEVEL_ELIGIBLE_MODULES)
        распознавание признаёт компонент на уровне модуля: element=None,
        исходный фрагмент сохранён как есть."""
        html = self._clean("Шапки", "Москва 1")
        result = lr.recognize_components(html, self.library)
        self.assertEqual(len(result.matches), 1, result.unresolved)
        m = result.matches[0]
        self.assertEqual(m.module, "Шапки")
        self.assertIsNone(m.element)
        self.assertEqual((m.start, m.end), (0, len(html)))
        self.assertEqual(m.html, html)
        self.assertEqual(result.unresolved, [])

    def test_footer_family_resolves_as_module_level_component(self):
        """Тот же факт, что и для шапок (см. предыдущий тест), для подвалов."""
        html = self._clean("Подвалы", "Школа 1")
        result = lr.recognize_components(html, self.library)
        self.assertEqual(len(result.matches), 1, result.unresolved)
        m = result.matches[0]
        self.assertEqual(m.module, "Подвалы")
        self.assertIsNone(m.element)
        self.assertEqual((m.start, m.end), (0, len(html)))
        self.assertEqual(m.html, html)
        self.assertEqual(result.unresolved, [])

    def test_single_component_content_block(self):
        html = self._clean("Дополнительные Баннеры", "Вариант 1")
        result = lr.recognize_components(html, self.library)
        self.assertEqual(len(result.matches), 1, result.unresolved)
        self.assertEqual(
            (result.matches[0].module, result.matches[0].element),
            ("Дополнительные Баннеры", "Вариант 1"),
        )

    def test_several_components_in_order_with_correct_boundaries(self):
        """Границы и порядок: несколько компонентов подряд в одном "письме" —
        должны быть найдены все, в правильном порядке, без наложения и без
        поглощения соседнего компонента. Намеренно взяты компоненты с
        конкретным (не module-level) element — порядок/границы для
        module-level компонентов (Шапки/Подвалы) отдельно проверены в
        ModuleLevelRecognitionTests."""
        parts = [
            ("Мероприятия", "Расписание 1"),
            ("Мероприятия", "Расписание 2"),
            ("Дополнительные Баннеры", "Вариант 1"),
        ]
        htmls = [self._clean(m, e) for m, e in parts]
        document = "<table><tbody>\n" + "\n".join(htmls) + "\n</tbody></table>"

        result = lr.recognize_components(document, self.library)
        self.assertEqual(len(result.matches), 3, result.unresolved)

        for expected, match in zip(parts, result.matches):
            self.assertEqual((match.module, match.element), expected)
        self.assertEqual([m.order for m in result.matches], [0, 1, 2])

        # границы не пересекаются и идут по возрастанию
        for a, b in zip(result.matches, result.matches[1:]):
            self.assertLessEqual(a.end, b.start)
        # каждый найденный фрагмент - это именно <tr>...</tr>, без соседей
        for match in result.matches:
            self.assertTrue(match.html.startswith("<tr"))
            self.assertTrue(match.html.rstrip().endswith("</tr>"))


class FullLibrarySweepTests(unittest.TestCase):
    """Самый строгий тест набора: прогоняет "чистый" (без letteros-*) экспорт
    каждого из 121 canonical-компонента через recognize_components() и
    проверяет главное инвариантное свойство этапа 1 — НИКОГДА не выдавать
    неверный ответ. Три допустимых исхода:
      - ровно одно совпадение с точным (module, element) — "уникально";
      - ровно одно совпадение (module, None) для module-level компонента
        (см. ModuleLevelRecognitionTests) — только для
        MODULE_LEVEL_ELIGIBLE_MODULES, и только если найденный module
        совпадает с истинным модулем этого компонента — "module-level";
      - ни одного совпадения, но истинный (module, element) обязан быть
        среди кандидатов в unresolved — "неоднозначно, без потери".
    Ложное совпадение, множественное совпадение, module-level результат для
    модуля вне MODULE_LEVEL_ELIGIBLE_MODULES или полная потеря компонента —
    провал."""

    def test_full_library_self_recognition_is_never_wrong(self):
        library = lr.load_letteros_library()
        wrong_matches = []
        lost_components = []
        unique_count = 0
        module_level_count = 0
        ambiguous_count = 0

        for key, entry in library.items():
            true_module, _true_element = key
            cleaned = _strip_letteros_attrs(entry.html)
            result = lr.recognize_components(cleaned, library)

            if len(result.matches) == 1:
                found_module, found_element = result.matches[0].module, result.matches[0].element
                if (found_module, found_element) == key:
                    unique_count += 1
                elif found_element is None and found_module == true_module and true_module in lr.MODULE_LEVEL_ELIGIBLE_MODULES:
                    module_level_count += 1
                else:
                    wrong_matches.append((key, (found_module, found_element)))
                continue

            if len(result.matches) > 1:
                wrong_matches.append((key, [(m.module, m.element) for m in result.matches]))
                continue

            candidates = result.unresolved[0].candidate_names if result.unresolved else []
            if key in candidates:
                ambiguous_count += 1
            else:
                lost_components.append(key)

        self.assertEqual(wrong_matches, [], f"неверные/множественные совпадения: {wrong_matches}")
        self.assertEqual(lost_components, [], f"компонент потерян полностью (не найден даже как кандидат): {lost_components}")
        # Регрессионная точная цифра: 15 элементов "Шапки" + 18 элементов
        # "Подвалы" = 33 — весь состав этих двух модулей теперь module-level
        # (ни один их элемент не входит в 81 "уникальных" — см. диагностику).
        self.assertEqual(module_level_count, 33)
        self.assertEqual(unique_count + module_level_count + ambiguous_count, 121)
        print(
            f"\n--- Полный прогон библиотеки (121 компонент): "
            f"однозначно распознано {unique_count}, module-level {module_level_count}, "
            f"распознано с неоднозначностью (верный вариант среди кандидатов, но не "
            f"единственный) {ambiguous_count}, ошибочно/потеряно 0 ---"
        )


class ModuleLevelRecognitionTests(unittest.TestCase):
    """Прицельные тесты на module-level recognition (см.
    letteros_recognition.MODULE_LEVEL_ELIGIBLE_MODULES) — отдельно от общих
    (SelfRecognitionOnCleanExportTests/FullLibrarySweepTests), чтобы явно
    проверить каждое требование по отдельности: element is None, границы и
    исходный HTML сохранены без изменений, отсутствие распространения
    module-level fallback на модули, для которых он не проверялся."""

    @classmethod
    def setUpClass(cls):
        cls.library = lr.load_letteros_library()

    def test_header_element_is_none(self):
        html = _strip_letteros_attrs(self.library[("Шапки", "Казань 2")].html)
        result = lr.recognize_components(html, self.library)
        self.assertEqual(len(result.matches), 1)
        self.assertIsNone(result.matches[0].element)
        self.assertIsInstance(result.matches[0].module, str)

    def test_footer_element_is_none(self):
        html = _strip_letteros_attrs(self.library[("Подвалы", "Казань 2")].html)
        result = lr.recognize_components(html, self.library)
        self.assertEqual(len(result.matches), 1)
        self.assertIsNone(result.matches[0].element)
        self.assertIsInstance(result.matches[0].module, str)

    def test_original_start_end_html_preserved_for_module_level_match(self):
        """Фрагмент, который отдаёт module-level match, должен быть исходным
        HTML письма как есть -- не подменённым каноническим вариантом и не
        обрезанным/дополненным."""
        header_html = _strip_letteros_attrs(self.library[("Шапки", "Москва 3")].html)
        footer_html = _strip_letteros_attrs(self.library[("Подвалы", "Москва 2")].html)
        document = header_html + "\n" + footer_html

        result = lr.recognize_components(document, self.library)
        self.assertEqual(len(result.matches), 2, result.unresolved)

        header_match, footer_match = result.matches
        self.assertEqual(header_match.module, "Шапки")
        self.assertEqual(footer_match.module, "Подвалы")

        # ровно исходный срез документа, без изменений
        self.assertEqual(header_match.html, document[header_match.start:header_match.end])
        self.assertEqual(footer_match.html, document[footer_match.start:footer_match.end])
        self.assertEqual(header_match.html, header_html)
        self.assertEqual(footer_match.html, footer_html)
        # это НЕ подмена каким-то другим canonical-вариантом того же модуля
        self.assertNotEqual(header_match.html, _strip_letteros_attrs(self.library[("Шапки", "Казань 1")].html))
        self.assertEqual(header_match.order, 0)
        self.assertEqual(footer_match.order, 1)
        self.assertLessEqual(header_match.end, footer_match.start)

    def test_module_level_fallback_does_not_apply_to_other_modules(self):
        """Требование 6: глобального ослабления нет. "Баннеры/Вариант 1"
        структурно неоднозначен внутри своего модуля точно так же, как
        Шапки/Подвалы (общая подпись с "Вариант 2", ни один из двух не
        проходит структурную проверку до единственного варианта — проверено
        отдельно), но "Баннеры" НЕ входит в MODULE_LEVEL_ELIGIBLE_MODULES —
        значит результат обязан остаться unresolved, а не стать
        module-level "Баннеры"."""
        self.assertNotIn("Баннеры", lr.MODULE_LEVEL_ELIGIBLE_MODULES)
        html = _strip_letteros_attrs(self.library[("Баннеры", "Вариант 1")].html)
        result = lr.recognize_components(html, self.library)
        self.assertEqual(result.matches, [])
        wrapper = next((u for u in result.unresolved if u.start == 0), None)
        self.assertIsNotNone(wrapper, result.unresolved)
        candidate_modules = {m for m, _e in wrapper.candidate_names}
        self.assertEqual(candidate_modules, {"Баннеры"})

    def test_eligible_modules_set_is_exactly_header_and_footer(self):
        # Прямая проверка значения константы -- случайное расширение набора
        # без отдельной диагностики (как для остальных 9 модулей библиотеки)
        # должно быть замечено при ревью, а не проскочить незамеченным.
        self.assertEqual(lr.MODULE_LEVEL_ELIGIBLE_MODULES, {"Шапки", "Подвалы"})


class NoFalsePositiveTests(unittest.TestCase):
    """Требование 3 из ТЗ: произвольный <tr> не должен считаться компонентом."""

    @classmethod
    def setUpClass(cls):
        cls.library = lr.load_letteros_library()

    def test_unrelated_tr_is_not_even_a_candidate(self):
        # подпись (align/bgcolor/padding/border-radius), которой нет ни у одного
        # из 121 canonical-компонентов (проверено при анализе библиотеки).
        html = (
            '<tr><td align="justify" bgcolor="#010203" '
            'style="background-color: #010203; padding: 1px; border-radius: 3px;">'
            "случайный текст, не имеющий отношения к библиотеке компонентов"
            "</td></tr>"
        )
        result = lr.recognize_components(html, self.library)
        self.assertEqual(result.matches, [])
        self.assertEqual(result.unresolved, [])  # даже не кандидат — подпись не совпала ни с чем

    def test_coincidental_signature_is_rejected_not_guessed(self):
        # Подпись совпадает с реальной группой кандидатов (общая для 21
        # компонента — см. аудит letteros_recognition.py), но внутреннее
        # содержимое произвольное — структурная проверка обязана отклонить.
        html = (
            '<tr><td align="center" bgcolor="#FFFFFF" '
            'style="background-color: #FFFFFF; padding: 6px 15px;">'
            "<div>это не структура ни одного canonical-компонента</div>"
            "</td></tr>"
        )
        result = lr.recognize_components(html, self.library)
        self.assertEqual(result.matches, [])
        self.assertEqual(len(result.unresolved), 1)
        unresolved = result.unresolved[0]
        self.assertGreater(len(unresolved.candidate_names), 1)  # подпись неоднозначная сама по себе
        self.assertTrue(all(r for r in unresolved.reasons))  # причина зафиксирована, не пропущена молча


class RealProjectLetterTests(unittest.TestCase):
    """Требование: прогон на реальном Letteros HTML проекта. Результат
    зафиксирован как есть, включая объяснение причины неполного совпадения —
    без ослабления проверки до угадывания (см. класс docstring и комментарии
    ниже)."""

    @classmethod
    def setUpClass(cls):
        if not REAL_LETTER_PATH.is_file():
            raise unittest.SkipTest(f"реальный файл не найден: {REAL_LETTER_PATH}")
        cls.library = lr.load_letteros_library()
        cls.raw = REAL_LETTER_PATH.read_text(encoding="utf-8")
        cls.result = lr.recognize_components(cls.raw, cls.library)

    def test_no_letteros_editor_attrs_in_real_letter(self):
        # Подтверждает исходную посылку модуля: реально отправленное письмо не
        # содержит editor-only атрибутов Letteros вообще.
        self.assertNotIn("letteros-", self.raw)

    def test_confirmed_matches_include_header_and_footer_module_level(self):
        # ЧЕСТНЫЙ факт на сегодня: среди подтверждённых совпадений на этом
        # файле обязательно есть module-level "Шапки" и "Подвалы" (element=
        # None). Остальные подтверждённые совпадения — экземпляры
        # "Разделители/Отступ между блоками 16px" (после нормализации
        # position:relative, см. _style_equal/_LETTEROS_NOISE_POSITION_VALUE) —
        # не тема этого теста. Содержимое письма помимо этого (карточка,
        # приветствие, CTA и т.п.) не состоит из известных canonical-
        # компонентов вовсе — не распознаётся (см. RealProductionLettersTests
        # для разбора причин по каждому нераспознанному фрагменту). Сам файл
        # при этом — не чистый HTTP-экспорт Letteros, а копия, пропущенная
        # через DOM браузера (см. bis_size — типичный след браузерного
        # расширения, модифицирующего DOM при сохранении страницы), что
        # раньше блокировало даже шапку/подвал через строгое сравнение по
        # element — теперь не блокирует благодаря module-level recognition.
        self.assertIn("bis_size", self.raw)
        found = {(m.module, m.element) for m in self.result.matches}
        self.assertIn(("Шапки", None), found)
        self.assertIn(("Подвалы", None), found)

    def test_header_and_footer_fragments_preserved_as_is(self):
        for m in self.result.matches:
            if m.match_mode == "wrapper_stripped":
                # По конструкции (см. letteros_recognition._resolve_wrapper_
                # stripped_match()): html — это найденный (возможно, более
                # глубокий, если между сканированной позицией и содержимым
                # были проходные обёртки-<table> без содержания) уровень, а
                # start/end по-прежнему охватывают ВЕСЬ отсканированный
                # фрагмент целиком (иначе текст проходных обёрток остаётся
                # вне диапазона любого match — "разрыв" в _check_no_gaps()).
                # Поэтому здесь m.html — подстрока raw[start:end], а не равна
                # ей целиком.
                self.assertIn(m.html, self.raw[m.start:m.end])
                continue
            self.assertEqual(m.html, self.raw[m.start:m.end])
            self.assertTrue(m.html.startswith("<tr"))
            self.assertTrue(m.html.rstrip().endswith("</tr>"))


class NormalizationTests(unittest.TestCase):
    """Регрессионные тесты на обе нормализации, добавленные после диагностики
    на 4 реальных Letteros-письмах (см. чат/отчёт к этому изменению):
    неявный <tbody> и пустой class="" там, где canonical-компонент class не
    задаёт вовсе. Обе — не ослабление structural matching: проверяют, что
    именно эти два случая перестали блокировать совпадение, и что реальные
    (содержательные) отличия по-прежнему блокируют его."""

    @classmethod
    def setUpClass(cls):
        cls.library = lr.load_letteros_library()

    def test_tbody_wrapped_instance_still_matches(self):
        canon = self.library[("Мероприятия", "Расписание 2")].html
        instance = _strip_letteros_attrs(canon)
        # Имитация того, что происходит в реальных письмах (см. диагностику):
        # каждый <table>...</table> получает неявный <tbody>.
        wrapped = re.sub(r"(<table\b[^>]*>)", r"\1<tbody>", instance)
        wrapped = wrapped.replace("</table>", "</tbody></table>")
        ok, diffs = lr.structural_match(canon, wrapped)
        self.assertTrue(ok, diffs)

    def test_tbody_normalization_does_not_hide_real_differences(self):
        """<tbody> снимается, но если внутри реально другое содержимое —
        совпадение всё равно не проходит."""
        canon = self.library[("Мероприятия", "Расписание 2")].html
        instance = _strip_letteros_attrs(canon)
        wrapped = re.sub(r"(<table\b[^>]*>)", r"\1<tbody>", instance)
        wrapped = wrapped.replace("</table>", "</tbody></table>")
        wrapped_broken = wrapped.replace('bgcolor="#FFFFFF"', 'bgcolor="#EAF5F4"', 1)
        ok, diffs = lr.structural_match(canon, wrapped_broken)
        self.assertFalse(ok, "изменённый bgcolor должен блокировать совпадение даже после снятия tbody")

    def test_empty_class_on_untagged_node_is_ignored(self):
        canon = self.library[("Разделители", "Отступ между блоками 16px")].html
        instance = _strip_letteros_attrs(canon).replace('<td', '<td class=""', 1)
        ok, diffs = lr.structural_match(canon, instance)
        self.assertTrue(ok, diffs)

    def test_meaningful_class_mismatch_is_not_ignored(self):
        """class="" снимается только когда canon вообще не задаёт class.
        Настоящее несовпадающее значение class по-прежнему блокирует
        совпадение — это не общее ослабление сравнения class."""
        canon = self.library[("Дополнительные Баннеры", "Вариант 1")].html
        # в этом компоненте есть узел с class="mob_100" — подменяем реальное
        # значение на другое непустое
        self.assertIn('class="mob_100"', canon)
        instance = _strip_letteros_attrs(canon).replace('class="mob_100"', 'class="something_else"', 1)
        ok, diffs = lr.structural_match(canon, instance)
        self.assertFalse(ok, "несовпадающее непустое значение class должно блокировать совпадение")
        self.assertTrue(any("class" in d for d in diffs), diffs)


class PositionRelativeNormalizationTests(unittest.TestCase):
    """Регрессионные тесты на нормализацию добавленного "position: relative"
    (см. _style_equal/_LETTEROS_NOISE_POSITION_VALUE и специальный случай в
    _nodes_match для style, когда canon вообще не задаёт этот атрибут) —
    диагностика: один и тот же canonical-компонент в одном и том же реальном
    письме встречается и с этим свойством, и без; в canonical-библиотеке
    "position: relative" на живых тегах не встречается ни разу (единственное
    вхождение — внутри MSO-комментария, вне области действия этой логики)."""

    def test_canon_without_position_instance_with_relative_matches(self):
        canon = '<tr letteros-element="X" letteros-module="Y"><td><table width="100%"><tr><td>x</td></tr></table></td></tr>'
        instance = '<tr><td><table width="100%" style="position: relative;"><tr><td>x</td></tr></table></td></tr>'
        ok, diffs = lr.structural_match(canon, instance)
        self.assertTrue(ok, diffs)

    def test_canon_without_position_instance_without_position_matches(self):
        canon = '<tr letteros-element="X" letteros-module="Y"><td><table width="100%"><tr><td>x</td></tr></table></td></tr>'
        instance = '<tr><td><table width="100%"><tr><td>x</td></tr></table></td></tr>'
        ok, diffs = lr.structural_match(canon, instance)
        self.assertTrue(ok, diffs)

    def test_canon_without_position_instance_with_absolute_does_not_match(self):
        canon = '<tr letteros-element="X" letteros-module="Y"><td><table width="100%"><tr><td>x</td></tr></table></td></tr>'
        instance = '<tr><td><table width="100%" style="position: absolute;"><tr><td>x</td></tr></table></td></tr>'
        ok, diffs = lr.structural_match(canon, instance)
        self.assertFalse(ok, "position: absolute не эквивалентно отсутствию position")

    def test_canon_with_relative_instance_with_different_position_does_not_match(self):
        canon = '<tr letteros-element="X" letteros-module="Y"><td><table width="100%" style="position: relative;"><tr><td>x</td></tr></table></td></tr>'
        instance = '<tr><td><table width="100%" style="position: absolute;"><tr><td>x</td></tr></table></td></tr>'
        ok, diffs = lr.structural_match(canon, instance)
        self.assertFalse(ok, "canon с position:relative и инстанс с другим значением не должны совпадать")

    def test_canon_with_position_instance_without_position_does_not_match(self):
        """Нормализация односторонняя: canon ЗАДАЁТ position -- отсутствие
        его у инстанса по-прежнему различие (условие "position not in
        canon_attrs" ложно, особый случай не срабатывает)."""
        canon = '<tr letteros-element="X" letteros-module="Y"><td><table width="100%" style="position: relative;"><tr><td>x</td></tr></table></td></tr>'
        instance = '<tr><td><table width="100%"><tr><td>x</td></tr></table></td></tr>'
        ok, diffs = lr.structural_match(canon, instance)
        self.assertFalse(ok)

    def test_extra_real_css_alongside_position_relative_is_not_ignored(self):
        """Если помимо "position: relative" в style инстанса есть ЕЩЁ
        какое-то реальное CSS-свойство, которого нет в canon, -- style
        по-прежнему считается отличием (снимается только "position:
        relative" и ничего больше, п.2 требования)."""
        canon = '<tr letteros-element="X" letteros-module="Y"><td><table width="100%"><tr><td>x</td></tr></table></td></tr>'
        instance = '<tr><td><table width="100%" style="position: relative; color: red;"><tr><td>x</td></tr></table></td></tr>'
        ok, diffs = lr.structural_match(canon, instance)
        self.assertFalse(ok, "лишнее CSS-свойство color:red не должно молча игнорироваться вместе с position")

    def test_position_relative_inside_mso_vml_comment_not_silently_normalized(self):
        """position: relative внутри MSO/VML-комментария -- это часть текста
        комментария (сравнивается через _normalize_comment_whitespace), а не
        style живого тега; эта нормализация его не касается вообще -- лишний
        "position: relative" внутри комментария там, где в canon его нет,
        должен остаться настоящим отличием, а не молча пройти."""
        canon = ('<tr letteros-element="X" letteros-module="Y"><td>'
                 '<!--[if gte mso 9]><v:oval></v:oval><![endif]--><span>t</span></td></tr>')
        instance = ('<tr><td>'
                    '<!--[if gte mso 9]><v:oval style="position: relative;"></v:oval><![endif]-->'
                    '<span>t</span></td></tr>')
        ok, diffs = lr.structural_match(canon, instance)
        self.assertFalse(ok, "добавленный текст внутри комментария -- это изменение контента комментария, не style")

    def test_real_spacer_with_position_relative_is_recognized(self):
        """Требования 6/7: реальный "Разделители/Отступ между блоками 16px"
        с добавленным production-шумом position:relative распознаётся, и оба
        варианта (с этим свойством и без) распознаются как один и тот же
        canonical component."""
        library = lr.load_letteros_library()
        canon = library[("Разделители", "Отступ между блоками 16px")].html
        with_position = _strip_letteros_attrs(canon).replace(
            '<table border="0" cellpadding="0" cellspacing="0" width="100%">\n<tr><td height="16"',
            '<table border="0" cellpadding="0" cellspacing="0" style="position: relative;" width="100%">\n<tr><td height="16"',
            1,
        )
        self.assertIn("position: relative", with_position)  # sanity: подмена реально сработала
        without_position = _strip_letteros_attrs(canon)

        for label, html in [("с position:relative", with_position), ("без", without_position)]:
            result = lr.recognize_components(html, library)
            self.assertEqual(len(result.matches), 1, (label, result.unresolved))
            self.assertEqual(
                (result.matches[0].module, result.matches[0].element),
                ("Разделители", "Отступ между блоками 16px"),
                label,
            )

    def test_both_real_spacer_variants_recognized_in_same_real_letter(self):
        """То же самое требование 7, но не на искусственно собранном
        компоненте, а на настоящем письме проекта, где оба варианта (с
        position:relative и без) физически присутствуют одновременно."""
        library = lr.load_letteros_library()
        fp = REAL_LETTERS_DIR / "ГИ special рассылка 21.04.2026.html"
        raw = fp.read_text(encoding="utf-8")
        self.assertIn('style="position: relative;"', raw)  # sanity: обе формы есть в файле

        result = lr.recognize_components(raw, library)
        spacer_matches = [
            m for m in result.matches
            if (m.module, m.element) == ("Разделители", "Отступ между блоками 16px")
        ]
        self.assertGreaterEqual(len(spacer_matches), 2, "должны найтись экземпляры обеих форм")
        # среди найденных действительно есть и с position:relative, и без
        has_with = any("position: relative" in m.html for m in spacer_matches)
        has_without = any("position: relative" not in m.html for m in spacer_matches)
        self.assertTrue(has_with, "не нашли ни одного распознанного экземпляра с position:relative")
        self.assertTrue(has_without, "не нашли ни одного распознанного экземпляра без position:relative")
        # фактический HTML каждого распознанного экземпляра сохранён как есть
        # -- кроме wrapper_stripped (класс 1): там html может быть более
        # глубоким уровнем, чем весь отсканированный диапазон start:end (см.
        # letteros_recognition._resolve_wrapper_stripped_match()).
        for m in spacer_matches:
            if m.match_mode == "wrapper_stripped":
                # <tbody> снимается при разборе (см. _strip_implicit_tbody() /
                # _single_wrapped_table_rows()) -- сравниваем без него с обеих
                # сторон, чтобы не зависеть от того, добавляют ли конкретные
                # проходные обёртки этого экземпляра неявный <tbody> или нет.
                self.assertIn(lr._strip_implicit_tbody(m.html), lr._strip_implicit_tbody(raw[m.start:m.end]))
                continue
            self.assertEqual(m.html, raw[m.start:m.end])


class RealProductionLettersTests(unittest.TestCase):
    """5 реальных production Letteros-писем (mail_project/letteros htmls/),
    добавленные как fixtures для этого этапа (изначально 4, пятое —
    "Рассылка 17.09.2026.html" — добавлено позже, при диагностике
    FORM/CONTENT-архитектуры recognition, см. RECOGNITION_ARCHITECTURE_
    AUDIT.md). Числа ниже — не целевые показатели, а зафиксированный
    регрессионный результат текущего recognition (fingerprint/candidate index
    + FormModel FORM/CONTENT/TOLERATED/OPTIONAL + двух нормализаций tbody/
    пустой class) на реальных данных: честно невысокий (компоненты этих писем
    существенно отличаются от библиотеки содержательно, не только по
    DOM-артефактам — см. диагностику), но ни разу не бывший ошибочным (см.
    test_no_wrong_matches_on_real_letters)."""

    @classmethod
    def setUpClass(cls):
        if not REAL_LETTERS_DIR.is_dir():
            raise unittest.SkipTest(f"каталог с реальными письмами не найден: {REAL_LETTERS_DIR}")
        cls.library = lr.load_letteros_library()
        cls.files = sorted(REAL_LETTERS_DIR.glob("*.html"))
        if not cls.files:
            raise unittest.SkipTest(f"в {REAL_LETTERS_DIR} нет .html файлов")
        cls.results = {fp.name: lr.recognize_components(fp.read_text(encoding="utf-8"), cls.library) for fp in cls.files}

    def test_five_fixtures_present(self):
        self.assertEqual(len(self.files), 5)

    def test_no_crash_and_at_least_one_candidate_per_letter(self):
        for name, result in self.results.items():
            total = len(result.matches) + len(result.unresolved)
            self.assertGreater(total, 0, f"{name}: ни одного кандидата вообще — подозрительно для реального Letteros-письма")

    def test_no_wrong_matches_on_real_letters(self):
        """Единственное по-настоящему критичное свойство на этом этапе: то,
        что подтверждено (matches), должно быть подтверждено ПРАВИЛЬНО.

        Для обычных ("full", element задан) совпадений — сравниваем с
        исходным строгим structural_match ещё раз, без доверия к внутреннему
        состоянию recognize_components. Для module-level совпадений
        (element=None: Шапки/Подвалы — MODULE_LEVEL_ELIGIBLE_MODULES,
        Мероприятия — SCHEDULE_MODULE_NAME (класс 2), или Контентные блоки —
        GRID_MODULE_NAME (класс 4)) строгого совпадения с конкретным
        canonical-элементом не требуется по определению — вместо этого
        проверяем то, что действительно должно быть гарантировано: module —
        один из этих модулей, а html — ровно исходный срез письма
        (m.start:m.end], без подмены (для класса 4 дополнительно — что
        extract_grid_items() повторно извлекает ровно 2 item'а). Для
        "wrapper_stripped" (класс 1) — строгий structural_match со
        СТРИППЕД-версией canonical (то же расширение допусков для image-slot,
        что и в recognize_components()). Для "heading_text_injection"
        (класс 3) — structural_match заведомо не пройдёт (production
        сознательно отличается по <strong>/class/line-height, см.
        диагностику) — проверяем то, что реально гарантировано: font-size
        совпадает и текст извлекается."""
        for name, result in self.results.items():
            raw = next(fp for fp in self.files if fp.name == name).read_text(encoding="utf-8")
            for m in result.matches:
                if m.element is None:
                    self.assertIn(
                        m.module,
                        lr.MODULE_LEVEL_ELIGIBLE_MODULES | {lr.SCHEDULE_MODULE_NAME, lr.GRID_MODULE_NAME},
                        f"{name}: {m.module}",
                    )
                    self.assertEqual(m.html, raw[m.start:m.end], f"{name}: {m.module} module-level html подменён")
                    if m.module == lr.GRID_MODULE_NAME:
                        items = lr.extract_grid_items(m.html)
                        self.assertIsNotNone(items, f"{name}: grid item'ы не извлекаются повторно")
                        self.assertEqual(len(items), 2, f"{name}: ожидалось 2 item'а")
                    continue
                canon = self.library[(m.module, m.element)].html
                if m.match_mode == "wrapper_stripped":
                    stripped_canon = lr.extract_wrapper_stripped_inner(canon)
                    self.assertIsNotNone(stripped_canon, f"{name}: {m.module}/{m.element}")
                    if lr._is_single_image_slot_shape(stripped_canon):
                        ok, diffs = lr.structural_match(
                            stripped_canon, m.html,
                            lr._IMAGE_SLOT_EXTRA_VARIABLE_ATTRS, lr._IMAGE_SLOT_EXTRA_VARIABLE_STYLE_PROPS,
                        )
                    else:
                        ok, diffs = lr.structural_match(stripped_canon, m.html)
                    self.assertTrue(ok, f"{name}: {m.module}/{m.element} (wrapper_stripped): {diffs}")
                elif m.match_mode == "heading_text_injection":
                    sizes = lr._build_heading_font_sizes(self.library)
                    self.assertIn((m.module, m.element), sizes, f"{name}: {m.module}/{m.element}")
                    text = lr.extract_heading_text(m.html)
                    self.assertTrue(text, f"{name}: {m.module}/{m.element} — текст не извлечён")
                else:
                    # form_model этой же записи -- та же классификация,
                    # которую recognize_components() уже использовала при
                    # первом подтверждении (см. RECOGNITION_ARCHITECTURE_
                    # AUDIT.md, шаг 1-2): без неё повторная проверка была бы
                    # строже исходной (не учла бы CONTENT/TOLERATED-роли) и
                    # ложно проваливала бы уже правильно подтверждённые
                    # match'и (например, content-leaf с <strong> в тексте).
                    form_model = self.library[(m.module, m.element)].form_model
                    ok, diffs = lr.structural_match(canon, m.html, form_model=form_model)
                    self.assertTrue(ok, f"{name}: {m.module}/{m.element} числится подтверждённым, но повторная проверка не проходит: {diffs}")

    def test_confirmed_counts_regression_baseline(self):
        # ВАЖНО: это regression baseline ТОЛЬКО для этих 5 конкретных real
        # fixtures ("letteros htmls/") — не production-правило. Эти числа
        # нигде не используются production-кодом (letteros_recognition.py/
        # letteros_adapt.py/letteros_migrate.py/generation.py) для принятия
        # решений — recognize_components() не хранит и не сверяется ни с
        # каким ожидаемым количеством блоков; оно вычисляется исключительно
        # динамически через структурное сравнение с library в момент вызова.
        # Значения ниже НЕ определяют допустимое/минимальное/максимальное
        # число блоков письма вообще — это просто "что фактически нашлось на
        # этих 5 файлах сегодня".
        #
        # Зафиксировано на: FORM/CONTENT/TOLERATED/OPTIONAL FormModel + единый
        # generic candidate index (build_candidate_index()/find_candidates(),
        # см. RECOGNITION_ARCHITECTURE_AUDIT.md) + tbody-нормализация + пустой
        # class="" + module-level recognition для Шапки/Подвалы/Мероприятия +
        # нормализация "position: relative" + классы 1-4 (wrapper_stripped/
        # heading_text_injection/grid_content_injection/schedule module-level).
        # Менять эти числа можно только осознанно — вместе с изменением
        # recognition/нормализации, и только проверив ПРИЧИНУ изменения
        # (новая корректная нормализация — ок; случайное ослабление строгости
        # сравнения — нет).
        expected_confirmed = {
            "ГИ special рассылка 21.04.2026.html": 27,
            "Казань Рассылка 30.07.2026.html": 13,
            "Копия — Мск 27.11.2025 рассылка.html": 16,
            "Рассылка 17.09.2026.html": 17,
            "Франшиза — экскурсия.html": 12,
        }
        actual = {name: len(result.matches) for name, result in self.results.items()}
        self.assertEqual(actual, expected_confirmed)

    def test_every_letter_has_header_and_footer_as_module_level(self):
        for name, result in self.results.items():
            found = {(m.module, m.element) for m in result.matches}
            self.assertIn(("Шапки", None), found, name)
            self.assertIn(("Подвалы", None), found, name)


def _print_real_letters_summary():
    if not REAL_LETTERS_DIR.is_dir():
        return
    lib = lr.load_letteros_library()
    print("\n--- 4 реальных production-письма ---")
    for fp in sorted(REAL_LETTERS_DIR.glob("*.html")):
        raw = fp.read_text(encoding="utf-8")
        result = lr.recognize_components(raw, lib)
        total = len(result.matches) + len(result.unresolved)
        print(f"\n{fp.name}: кандидатов={total}, подтверждено={len(result.matches)}, нераспознано={len(result.unresolved)}")
        for m in result.matches:
            print(f"   CONFIRMED [{m.order}] {m.module}/{m.element}")
        big = sorted(result.unresolved, key=lambda u: -(u.end - u.start))[:5]
        for u in big:
            mods = sorted({mm for mm, _ in u.candidate_names})
            print(f"   unresolved [{u.start}:{u.end}] modules={mods} reason0={u.reasons[0][:140]}")


def _print_human_summary():
    lib = lr.load_letteros_library()
    raw = REAL_LETTER_PATH.read_text(encoding="utf-8")
    result = lr.recognize_components(raw, lib)
    print("\n--- Реальное письмо проекта:", REAL_LETTER_PATH.name, "---")
    print(f"Подтверждено распознано: {len(result.matches)}")
    for m in result.matches:
        print(f"  [{m.order}] {m.module}/{m.element} [{m.start}:{m.end}]")
    print(f"Кандидатов без подтверждения: {len(result.unresolved)}")
    for u in result.unresolved:
        modules = sorted({m for m, _ in u.candidate_names})
        print(f"  [{u.start}:{u.end}] модуль(и)-кандидат(ы): {modules}, причина (первая): {u.reasons[0][:160]}")


if __name__ == "__main__":
    unittest.main(verbosity=2, exit=False)
    if REAL_LETTER_PATH.is_file():
        _print_human_summary()
    _print_real_letters_summary()
