# Applying a firmware update · Як оновити прошивку

The canonical guide for updating **OSELIA Hearth** gateway firmware. Every release links
here. Firmware ships **over Ethernet** through the OSELIA Home Assistant integration — no
USB, no tools. For almost everyone an update is one click; the notes below also cover what
happens under the hood and the rare recovery case.

<details open>
<summary><b>🇺🇦 Українською</b></summary>

<br>

### 1. Встановіть оновлення
У Home Assistant відкрийте пристрій (**Settings → Devices & Services → OSELIA → ваш шлюз**).
Коли доступна нова прошивка, HA показує картку **«Firmware update available»** →
натисніть **Install (Встановити)**. HA завантажує образ і передає його на шлюз через
локальний MQTT-брокер; пристрій перезавантажується у нову версію.

### 2. Більше нічого робити не треба
Після перезавантаження шлюз сам перепідключається: сутності, події входів і автоматизації
повертаються автоматично. Нічого вводити заново не потрібно.

### 3. Це безпечно (A/B + авто-відкат)
Оновлення використовує дві копії прошивки (A/B) із перевіркою завантаження:
- Якщо нова збірка не стартує або не виходить на зв'язок зі шлюзом — пристрій **сам
  повертається** до попередньої робочої версії. «Цеглини» бути не може.
- Збій живлення під час завантаження **не чіпає** робочу версію — наступний старт піде зі
  старої прошивки.
- Ваші налаштування зберігаються: ідентичність і доступи шлюзу (`site.json`), а також
  тюнінг (тривалість довгого/подвійного натиску, антидребезг, назви входів) переживають
  оновлення.

### 4. Якщо щось пішло не так
Зачекайте ~1 хвилину — якщо нова версія не «підтвердилась», шлюз сам відкотиться і
повернеться онлайн на старій прошивці. Стан оновлення видно у статусі пристрою в HA. Якщо
шлюз не повертається онлайн зовсім — перевірте Ethernet/живлення; як крайній засіб
застосовується відновлення через USB (див. `firmware/FLASHING.md`).

### Перевірка
Після оновлення версія прошивки у картці пристрою відповідає новому релізу, індикатор
світиться зеленим (норма), а входи й автоматизації працюють.

</details>

<details>
<summary><b>🇬🇧 English</b></summary>

<br>

### 1. Install the update
In Home Assistant open the device (**Settings → Devices & Services → OSELIA → your
gateway**). When a new build is available HA shows a **"Firmware update available"** card →
click **Install**. HA downloads the bundle and streams it to the gateway over the local
MQTT broker; the device reboots into the new version.

### 2. Nothing else to do
After the reboot the gateway reconnects on its own — entities, input events, and
automations come back automatically. There is nothing to re-enter.

### 3. It's safe (A/B + auto-revert)
Updates use two firmware copies (A/B) with a boot-confirm gate:
- If the new build won't start or can't reach the gateway's broker, the device **reverts
  itself** to the previous working version. It cannot brick.
- A power cut mid-download **leaves the running version untouched** — the next boot runs the
  old firmware.
- Your settings are preserved: the gateway's identity/credentials (`site.json`) and your
  tuning (long-press / double-tap / debounce timings, input names) survive the update.

### 4. If something looks wrong
Give it ~1 minute — if the new build doesn't "confirm" itself, the gateway auto-reverts and
comes back online on the previous firmware. The update stage is visible in the device's
status in HA. If the gateway doesn't come back online at all, check Ethernet/power; as a
last resort it can be recovered over USB (see `firmware/FLASHING.md`).

### Verify
After the update the firmware version on the device card matches the new release, the status
LED is green (healthy), and inputs/automations work.

</details>
