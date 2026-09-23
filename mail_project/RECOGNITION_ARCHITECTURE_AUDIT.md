# RECOGNITION_ARCHITECTURE_AUDIT.md

Архитектурный аудит `letteros_recognition.py`/`letteros_adapt.py`/`letteros_migrate.py` — итог разбора, зачем это сделано и к какой конечной архитектуре нужно прийти, вместо продолжения добавлять по одному fallback-механизму на каждый новый production STOP. Документ фиксирует состояние на момент аудита (сразу после того, как был добавлен text-padding resolver для `Текстовые блоки/Текст NNpx`). Ничего в коде на момент написания не менялось.

## 1. Текущий pipeline

```
Letteros production HTML
        │
        ▼
load_letteros_library()  ── парсит 121 canonical Letteros-компонент,
        │                     считает per-entry: fingerprint, stripped_fingerprint,
        │                     content_leaf_spans
        ▼
recognize_components(html, library)
        │
        ├─ строит 5 ОТДЕЛЬНЫХ, независимо написанных индексов из одной library:
        │    _build_fingerprint_index()             (полный внешний td)
        │    _build_stripped_fingerprint_index()     (класс 1: td без обвязки)
        │    _build_heading_font_sizes()              (класс 3: font-size заголовков)
        │    _build_schedule_header_signatures()      (класс 2: day-шапка расписания)
        │    _build_text_padding_agnostic_index()      (последний фикс: без padding)
        │
        └─ скан <tr><td>, для каждого фрагмента, ПО ОЧЕРЕДИ:
             1. точный fingerprint → structural_match(+content_leaf_spans)
                  confirmed==1            → MATCH match_mode="full"
                  confirmed>1, 1 модуль ∈ {Шапки,Подвалы} → MATCH module-level
             2. (если #1 пусто) stripped-fingerprint, рекурсивный спуск (depth≤3)
                  → MATCH match_mode="wrapper_stripped"
             3. (если #1 непусто) font-size заголовка
                  → MATCH match_mode="heading_text_injection"
             4. (если #1 непусто, модуль=Контентные блоки) grid-маркеры
                  → MATCH match_mode="grid_content_injection"
             5. (если #1 непусто) day-шапка расписания
                  → MATCH module="Мероприятия", element=None
             6. (если #1 ПУСТО) text-padding-agnostic → нормализация padding →
                  structural_match() → MATCH match_mode="full"
             иначе → UNRESOLVED (если кандидаты были) / молчаливый gap (их не было)
        │
        ▼
RecognitionResult(matches, unresolved)
        │
        ▼
letteros_migrate.migrate_letter()
        _check_no_unresolved() / _check_no_gaps()   -- fail fast
        _split_schedule_and_determine_insertion()
        _adapt_all() → по match_mode на каждый match:
             "full"                   → adapt_wrapper() [+ content-leaf strip, если есть spans]
             "wrapper_stripped"        → _rebuild_wrapper_stripped()  (свой rebuild)
             "heading_text_injection"  → _rebuild_heading_text_injection() (свой rebuild)
             "grid_content_injection"  → _rebuild_grid_content_injection() (свой rebuild)
        _build_schedule_blocks() → generation.build_schedule_blocks()
        generation.build_email_html()
        ▼
MigrationResult(html, components, schedule_inserted_at, schedule_dates)
```

## 2. Где механизмы дублируют одну и ту же концепцию

**А. "Узкий индекс → confirm через structural_match"** — реализовано **5 раз** независимо (fingerprint / stripped-fingerprint / heading font-size / schedule signature / text-padding-agnostic), каждый раз с собственной функцией построения индекса и собственной логикой подтверждения, хотя итоговый confirm-шаг у всех, кроме heading/schedule, один и тот же — `structural_match()`.

**Б. "Это позиция — CONTENT, не FORM"** — реализовано **4 раза на разных уровнях** гранулярности, каждый раз отдельным механизмом:
- значение атрибута (`_VARIABLE_ATTRS`: img/src, img/alt, a/href, td/background);
- значение CSS-свойства (`_VARIABLE_STYLE_PROPS`: background-image);
- содержимое HTML-комментария (VML `<v:image>` regex);
- поддерево текста (`content_leaf_spans`, самый новый и самый общий).

Все четыре — один и тот же факт ("здесь canonical допускает вариативность, не влияющую на идентичность"), выраженный четырьмя разными структурами данных и четырьмя разными точками подключения в `_nodes_match`/`_style_equal`.

**В. "Значение атрибута варьируется у ОДНОГО И ТОГО ЖЕ компонента, не меняя его"** — реализовано **дважды**: `_LETTEROS_NOISE_POSITION_VALUE` (одно CSS-значение одного свойства, глобально) и весь свежепостроенный text-padding resolver (отдельный индекс + отдельная функция нормализации + отдельная confirm-петля, узко для одного семейства). Тот же факт, два несовместимых по форме решения.

**Г. "Опциональное поддерево"** — реализовано **дважды несовместимо**: `letteros-hide` + `_is_hide_optional()` (canonical сам декларирует опциональность, работает в общем `_align()`) vs grid tag-pill'ы (`letteros-hide` теряется при экспорте → пришлось изобретать отдельный regex-маркерный механизм `<!-- item -->`/`<!-- item END-->` с ручным 0/1/2-ветвлением в адаптации). Тот же концепт, два независимых механизма.

**Д. В `letteros_adapt.py`** — 3 отдельные функции `_rebuild_*` (wrapper_stripped/heading/grid) делают структурно одно и то же: "взять canonical UniSender skeleton, подставить в него извлечённый production-контент на конкретных позициях" — но каждая написана вручную под свой случай (своя регэксп-якорная замена, свой способ найти границы).

## 3. Необходимое / ad hoc / объединяемое

| Механизм | Категория |
|---|---|
| `parse_root`/`Node`-дерево (qa.py) | **Необходимо** — общая инфраструктура |
| `structural_match`/`_nodes_match`/`_align` | **Необходимо** — единственный настоящий движок сравнения формы, уже спроектирован правильно (принимает `extra_variable_attrs`/`extra_variable_style_props`/`content_leaf_spans`) |
| `letteros-hide`/`_is_hide_optional` | **Необходимо** — canonical-декларативный, общий |
| `content_leaf_spans` | **Необходимо** — canonical-декларативный, общий, это уже прототип конечной модели |
| `_VARIABLE_ATTRS`/`_VARIABLE_STYLE_PROPS`/VML-правило | **Необходимо по сути, но фрагментировано** — 3 списка вместо 1 |
| wrapper_stripped (класс 1, рекурсивный поиск) | **Необходимо как отдельная стратегия поиска** — уже реализован ОБЩО (не под конкретные компоненты, работает через `stripped_fingerprint` для всех 121 сразу) |
| Мероприятия wholesale-replace (класс 2) | **Необходимо и корректно отдельно** — это не FORM/CONTENT конкретного элемента, а "это модуль расписания целиком, он будет выброшен" — другая задача по сути |
| Шапки/Подвалы module-level | **Легитимный факт, но захардкожен вручную** — должен быть выводим, не curated-множеством |
| `heading_text_injection` (класс 3) | **Ad hoc, уже избыточен** — появился только как обходной путь вокруг того, что `_nodes_match` не терпел `<strong>` в тексте; content-leaf решает ровно эту же задачу генерически |
| `_LETTEROS_NOISE_POSITION_VALUE` | **Ad hoc, узкий частный случай** той же идеи, что и text-padding resolver |
| text-padding resolver (последний фикс) | **Ad hoc, ручной прецедент** — сам является примером того самого роста числа фиксов, о котором предупреждает задача |
| grid_content_injection (класс 4) | **Частично необходимо, частично ad hoc** — accent-цвет как FORM-дискриминатор между Вариант 2/3/4 — легитимно; regex-маркерное извлечение item'ов — вынужденный костыль из-за потери `letteros-hide` |
| 3 `_rebuild_*` в `letteros_adapt.py` | **Объединяемы** — одна и та же операция "подстановка CONTENT в объявленные слоты" |

## 4–6. Что должно быть декларативным (FORM / CONTENT) и четырёхчастная классификация

**FORM** (объявляется на canonical-узле, вычисляется один раз при загрузке, как уже сделано для `content_leaf_spans`):
- имя тега, форма/количество детей (skeleton) — кроме позиций, объявленных CONTENT/OPTIONAL;
- набор обязательных атрибутов (минус уже известные editor-only);
- значения атрибутов/style-свойств, НЕ объявленных CONTENT или TOLERATED.

**CONTENT** (тоже canonical-декларативно):
- текст внутри content-leaf узлов (с ограниченным enum inline-тегов);
- уже перечисленные `img[src]`/`img[alt]`/`a[href]`/`td[background]`/`background-image`/VML `<v:image src>`;
- геометрия картинки на позиции image-slot (width/max-width/border-radius) — CONTENT более узкого типа;
- повторяющийся блок (grid item) целиком — CONTENT на уровне поддерева, а не атрибута, выбираемый среди небольшого enum FORM-вариантов через дискриминатор (accent bgcolor).

**Четырёхчастная классификация**:
- **FORM identity** — различие означает ДРУГОЙ реальный элемент (bgcolor/padding/border-radius "внутренней плашки" у `Авторы`/`Мероприятия`/grid-accent'ов; font-size у текстовых/заголовочных вариантов; общий skeleton).
- **Tolerated FORM variation** — различие подтверждено (на реальных данных, не предположено) как НЕ меняющее идентичность (padding внешней card-обвязки у `Текст NNpx`; `position: relative`).
- **Content variation** — по определению не несёт идентичности (текст, ссылки, картинки, повторяющееся содержимое item'ов).
- **Optional structure** — узел может структурно отсутствовать целиком, по декларации canonical (`letteros-hide`) либо (когда этот сигнал теряется при экспорте, как у grid tag-pill'ов) по задокументированной альтернативной content-маркерной конвенции.

## 7. Место padding/align/bgcolor/border-radius в конечной модели

Единого ответа "эти атрибуты — шум" или "эти атрибуты — форма" **нет и не может быть** — доказано диагностикой: тот же `bgcolor`/`padding`/`border-radius` на **внешней card-обвязке** простого текстового блока — tolerated variation, а на **"внутренней плашке"** (`Мероприятия`/`Авторы`/grid item) — genuine FORM identity. Решение зависит от КОНКРЕТНОГО УЗЛА canonical-дерева, не от имени атрибута глобально.

Именно поэтому: (а) единый плоский fingerprint-tuple, применяемый одинаково ко всем 121 компонентам, — структурно неверная абстракция для чего-либо, кроме грубого пре-фильтра; (б) добавление нового исключения "по одному атрибуту на одно семейство за раз" (как было сделано с padding у `Текст NNpx`) — верно локально, но систематически не масштабируется. Правильное место этих атрибутов в конечной модели — быть частью **per-node** классификации (FORM/TOLERATED/CONTENT), а не отдельным глобальным или полу-глобальным правилом.

## 8. Нужен ли fingerprint

Да, но **только как ускоряющий пре-фильтр**, никогда как гейт допуска к сравнению. В конечной модели fingerprint должен строиться из атрибутов, объявленных FORM **для конкретной записи** (не единый фиксированный 5-tuple для всех 121) — то есть записи, которые допускают вариацию padding, просто не включают padding в свой собственный индексный ключ. Реальное решение всегда принимает один и тот же `structural_match()`.

## 9. Что удаляется после объединения

- **`heading_text_injection`** (класс 3, все 3 функции) — вероятно полностью избыточен после content-leaf; нужна проверка (не бланковое удаление), но кандидат №1.
- **Text-padding resolver** (только что добавленный, все 3 функции + отдельный индекс) — должен быть заменён общей per-node TOLERATED-декларацией.
- **`_LETTEROS_NOISE_POSITION_VALUE`** — тот же случай, сворачивается в общий механизм.
- Возможно — **отдельная структура данных** stripped-fingerprint-индекса (сама рекурсивная стратегия поиска остаётся, но её КЛЮЧИ должны браться из общей классификации, а не строиться отдельной функцией).

## 10. Оценка сложности конечного решения

Сегодня: **~11 отдельных механизмов** (fingerprint-индекс, stripped-fingerprint-индекс+поиск, heading-класс, schedule-класс, grid-класс, text-padding resolver, content-leaf, `_VARIABLE_ATTRS`, `_VARIABLE_STYLE_PROPS`, VML-правило, `position:relative`-правило, module-level Шапки/Подвалы-правило) + 4 отдельные `_rebuild_*`-функции в адаптации.

Конечная модель: **5–7 концепций**:
1. Единая per-node FORM/CONTENT/TOLERATED/OPTIONAL классификация (заменяет 6 из перечисленных выше: content-leaf, `_VARIABLE_ATTRS`, `_VARIABLE_STYLE_PROPS`, VML-правило, `position:relative`, text-padding).
2. Единый индекс кандидатов, построенный из этой классификации (заменяет вычисление ключей у fingerprint/stripped-fingerprint индексов).
3. Единый confirm-движок `structural_match`/`_nodes_match` (уже есть, не переписывается, только становится единственным потребителем классификации).
4. Стратегия "весь внешний wrapper опционален" (wrapper_stripped) — остаётся отдельной СТРАТЕГИЕЙ ПОИСКА (не сводится к п.1, это другой вопрос: "нет ли этого узла целиком", а не "отличается ли атрибут").
5. Стратегия "повторяющийся блок, выбираемый по FORM-дискриминатору" (grid) — тоже отдельная стратегия по той же причине.
6. Module-level wholesale-replace (Мероприятия) — отдельно и корректно, не про recognition конкретного элемента.
7. (опционально сворачивается в п.1 или остаётся отдельным) Module-level "элементы формально неразличимы" (Шапки/Подвалы).

Это **не сведение к одному механизму** — три из текущих стратегий (опциональный wrapper, дискриминатор-выбор повторяющегося блока, wholesale-replace) решают структурно разные задачи и не сворачиваются в "FORM MATCH → CONTENT EXTRACTION" буквально. Но они МОГУТ и ДОЛЖНЫ работать поверх ОДНОЙ общей классификации данных вместо трёх параллельных наборов hardcoded списков/индексов.

**Нужен ли рефакторинг** — да, честно: сам `structural_match`/`_nodes_match` **переписывать не нужно** (уже спроектирован правильно, уже принимает нужные параметры расширения). Переписать нужно **слой классификации** (сейчас разбросан по ~6 разным спискам/функциям, должен стать одной canonical-side декларацией) и **слой индексации** (сейчас 5 независимых index-builder'ов, должен стать одним, управляемым классификацией) и **слой инъекции в adapt** (сейчас 3+1 разные `_rebuild_*`, должны стать одной параметризованной операцией). Это умеренный рефакторинг двух-трёх слоёв, не переписывание всего pipeline с нуля — сканирующий цикл `recognize_components()`, `letteros_migrate.py`-оркестрация и `qa.py`-парсинг могут остаться в целом как есть.

---

## A. КОНЕЧНАЯ МОДЕЛЬ

1. Каждый canonical-узел (Letteros и UniSender, при загрузке библиотеки) получает единую декларативную роль: **FORM** / **CONTENT** / **TOLERATED FORM VARIATION** / **OPTIONAL** — вычисляется один раз из самого canonical HTML + (где нужно) кросс-проверки по всем 121 записям и по production fixtures, а не пишется вручную по одному правилу на один STOP.
2. Кандидатный индекс строится ИЗ этой классификации (только FORM-атрибуты конкретной записи), а не из одного глобального fingerprint-tuple, одинакового для всех.
3. Единственный confirm-движок — существующий `structural_match`/`_nodes_match`, управляемый той же классификацией; сам движок не растёт новыми параметрами под каждый новый случай.
4. CONTENT extraction — один общий механизм (обобщение `content_leaf_spans` + `_VARIABLE_ATTRS`/`_VARIABLE_STYLE_PROPS`/VML на все уровни: атрибут / style-свойство / поддерево-текст / поддерево-блок).
5. CONTENT injection — один общий механизм в адаптации (заменяет 4 `_rebuild_*`), параметризованный тем же описанием слотов.
6. Три отдельные СТРАТЕГИИ ПОИСКА остаются поверх общей классификации, не сворачиваясь в п.1–5: опциональный внешний wrapper целиком (wrapper_stripped); повторяющийся блок, выбираемый FORM-дискриминатором (grid); module-level wholesale-replace (расписание).
7. Padding/align/bgcolor/border-radius не имеют глобального статуса — их роль (FORM/TOLERATED/CONTENT) определяется узлом, на котором они встречены, как часть п.1.
8. Fingerprint — не гейт допуска, а ускоряющий пре-фильтр, производный от классификации, никогда не отклоняющий реального кандидата самостоятельно.

## B. ЧТО УДАЛЯЕМ / ОБЪЕДИНЯЕМ

- Удалить (после проверки на замену content-leaf): `heading_text_injection` целиком (`_build_heading_font_sizes`, `_try_heading_text_injection`, `extract_heading_text`, match_mode и rebuild в adapt).
- Удалить как самостоятельный механизм: text-padding resolver (`_build_text_padding_agnostic_index`, `_with_canonical_outer_padding`, `_resolve_text_padding_variant`) — заменить общей per-node TOLERATED-декларацией.
- Удалить как самостоятельный случай: `_LETTEROS_NOISE_POSITION_VALUE` — та же замена.
- Объединить в один индекс: `_build_fingerprint_index` + `_build_stripped_fingerprint_index` (ключи из общей классификации; сама рекурсивная стратегия поиска wrapper_stripped остаётся).
- Объединить в одно CONTENT-описание: `_VARIABLE_ATTRS` + `_VARIABLE_STYLE_PROPS` + VML-comment правило + `content_leaf_spans`.
- Объединить в одну injection-функцию: `_rebuild_wrapper_stripped` + `_rebuild_heading_text_injection` + `_rebuild_grid_content_injection` (+ уже сделанный content-leaf strip).

## C. ЧТО ОСТАЁТСЯ

- `parse_root`/`Node`/qa.py-инфраструктура.
- `structural_match`/`_nodes_match`/`_align` — ядро сравнения, без переписывания логики, только источник параметров меняется.
- `letteros-hide`/`_is_hide_optional` — как есть, уже декларативно.
- Стратегия "опциональный внешний wrapper" (текущий wrapper_stripped) — как отдельная стратегия поиска, но с ключами из общей классификации.
- Стратегия "grid: повторяющийся блок + FORM-дискриминатор (accent)" — как отдельная стратегия, маркерное извлечение item'ов остаётся (потеря `letteros-hide` при экспорте — фундаментальное ограничение данных, не решается обобщением).
- Module-level wholesale-replace для расписания — как отдельная, осознанно другая по смыслу задача.
- `letteros_migrate.py`-оркестрация (`_check_no_unresolved`/`_check_no_gaps`/`_split_schedule_and_determine_insertion`/сборка письма) — не про recognition, не затрагивается этим рефакторингом.

## D. ПЛАН РЕАЛИЗАЦИИ (крупные шаги, без кода)

1. **Спроектировать и материализовать единую per-node классификацию** (FORM/CONTENT/TOLERATED/OPTIONAL) как часть `LetterosEntry` — обобщение уже работающего `content_leaf_spans`, вбирающее в себя `_VARIABLE_ATTRS`/`_VARIABLE_STYLE_PROPS`/VML-правило/`position:relative`-правило/text-padding-tolerance как ДАННЫЕ, а не код.
2. **Переписать индексацию кандидатов** на один генерический построитель, управляемый этой классификацией, вместо 5 параллельных `_build_*_index` функций; сохранить стратегию "опциональный внешний wrapper" как отдельный поисковый проход поверх того же индекса.
3. **Свернуть 4 match_mode в адаптации** до одной параметризованной "inject CONTENT at declared slots" операции; оставить grid и schedule как осознанно отдельные стратегии (не пытаться их туда впихнуть).
4. **Верифицировать и удалить избыточные механизмы** (`heading_text_injection`, text-padding resolver, `_LETTEROS_NOISE_POSITION_VALUE`) — доказательно (regression на всех 5 fixtures + существующих тестах), не вслепую.
5. **Прогнать полную диагностику заново** на всех 5 production-писем после рефакторинга, чтобы получить новую, честную карту оставшихся реальных STOP — уже не как повод для нового ad hoc фикса, а как вход для калибровки классификации из п.1.
