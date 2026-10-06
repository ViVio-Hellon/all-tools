/**
 * 平板重量計算ツール
 * 平板の寸法 A・B と板厚から、面積・体積・重量を計算する(コイル計算と同じ仕組み)
 *
 *   面積(mm²) = A × B            面積(m²)  = 面積(mm²) / 1,000,000
 *   体積(mm³) = A × B × 板厚      体積(cm³) = 体積(mm³) / 1000
 *   重量(g)   = 体積(cm³) × 比重   重量(kg)  = 重量(g) / 1000
 */

class PlateCalculator {
  /**
   * 計算機クラスを初期化
   */
  constructor() {
    // 入力の範囲(画面の ※ 表示・グラフの X 軸と同じ)
    this.ranges = {
      a: { min: 0, max: 3000 },
      b: { min: 0, max: 10000 },
      t: { min: 0.05, max: 1000 },
      specificGravity: { min: 0.01, max: 100 }
    };

    this.initializeElements();

    // グローバル参照を追加(グラフ・3D から使う)
    window.plateCalculator = this;

    this.setupEventListeners();
    this.calculate();
  }

  /**
   * 要素の参照を取得
   */
  initializeElements() {
    const $ = (id) => document.getElementById(id);
    this.section = $('plate-section');

    this.aInput = $('plate-a');
    this.bInput = $('plate-b');
    this.tInput = $('plate-t');
    this.specificGravityInput = $('plate-specific-gravity');
    this.materialPreset = $('plate-material-preset');

    this.aError = $('plate-a-error');
    this.bError = $('plate-b-error');
    this.tError = $('plate-t-error');
    this.specificGravityError = $('plate-specific-gravity-error');

    this.weightResult = $('plate-weight-result');
    this.areaResult = $('plate-area-result');
    this.volumeResult = $('plate-volume-result');

    this.areaDetailed = $('plate-area-detailed');
    this.volumeDetailed = $('plate-volume-detailed');
    this.weightDetailed = $('plate-weight-detailed');

    this.modalVolumeMm = $('plate-modal-volume-mm');
    this.modalVolumeCm = $('plate-modal-volume-cm');
    this.modalWeightG = $('plate-modal-weight-g');
    this.modalWeightKg = $('plate-modal-weight-kg');
    this.modalAreaMm = $('plate-modal-area-mm');
    this.modalAreaM = $('plate-modal-area-m');
    this.modalVolumeMm2 = $('plate-modal-volume-mm2');
    this.modalVolumeCm2 = $('plate-modal-volume-cm2');
  }

  /**
   * 各種イベントリスナーを設定
   */
  setupEventListeners() {
    // 平板のタブ切り替え(コイルのタブとは別の名前にして、互いに影響しないようにする)
    const tabs = this.section.querySelectorAll('.ptab');
    const tabContents = this.section.querySelectorAll('.ptab-content');
    tabs.forEach(tab => {
      tab.addEventListener('click', () => {
        tabs.forEach(t => t.classList.remove('active'));
        tabContents.forEach(content => content.classList.remove('active'));
        tab.classList.add('active');
        document.getElementById(tab.getAttribute('data-ptab')).classList.add('active');
      });
    });

    // モーダル(開く・閉じる)は aluminum-coil-calculator.js が .details-btn / .modal を
    // まとめて扱っている(平板のモーダルも同じ部品)ので、ここでは繋がない

    // 材質プリセット
    this.materialPreset.addEventListener('change', () => {
      const selectedOption = this.materialPreset.options[this.materialPreset.selectedIndex];
      this.specificGravityInput.value = selectedOption.getAttribute('data-gravity');
      this.validateInput(this.specificGravityInput, this.ranges.specificGravity, this.specificGravityError);
      this.calculate();
    });

    // 入力フィールド
    const fields = [
      [this.aInput, this.ranges.a, this.aError],
      [this.bInput, this.ranges.b, this.bError],
      [this.tInput, this.ranges.t, this.tError],
      [this.specificGravityInput, this.ranges.specificGravity, this.specificGravityError]
    ];
    fields.forEach(([input, range, error]) => {
      input.addEventListener('input', () => {
        if (this.validateInput(input, range, error)) {
          this.calculate();
        }
      });
    });
  }

  /**
   * 入力値の検証を行う
   * @return {boolean} 検証結果
   */
  validateInput(input, range, errorElement) {
    const value = parseFloat(input.value);
    if (isNaN(value) || value < range.min || value > range.max) {
      input.classList.add('error');
      errorElement.style.display = 'block';
      return false;
    }
    input.classList.remove('error');
    errorElement.style.display = 'none';
    return true;
  }

  /**
   * いま画面に入っている値(すべて範囲内のときだけ)
   * @return {Object|null}
   */
  currentValues() {
    const values = {
      a: parseFloat(this.aInput.value),
      b: parseFloat(this.bInput.value),
      t: parseFloat(this.tInput.value),
      specificGravity: parseFloat(this.specificGravityInput.value)
    };
    for (const key of Object.keys(values)) {
      const v = values[key];
      if (isNaN(v) || v < this.ranges[key].min || v > this.ranges[key].max) return null;
    }
    return values;
  }

  /**
   * 数値をカンマ区切りで表示する(コイル計算と同じ)
   */
  formatNumber(number, decimals = 1) {
    return number.toFixed(decimals).replace(/\B(?=(\d{3})+(?!\d))/g, ",");
  }

  /**
   * 計算だけをする(表示しない)。グラフもこれを使う
   * @return {Object} 計算の途中と結果
   */
  static compute(a, b, t, specificGravity) {
    const areaMm = a * b;
    const areaM = areaMm / 1000000;
    const volumeMm = areaMm * t;
    const volumeCm = volumeMm / 1000;
    const weightGrams = volumeCm * specificGravity;
    const weightKg = weightGrams / 1000;
    return { areaMm, areaM, volumeMm, volumeCm, weightGrams, weightKg };
  }

  /**
   * 計算を実行して表示を更新する
   */
  calculate() {
    const values = this.currentValues();
    if (!values) return;
    const { a, b, t, specificGravity } = values;
    const r = PlateCalculator.compute(a, b, t, specificGravity);
    const f = (n, d) => this.formatNumber(n, d);

    // 結果値
    this.weightResult.textContent = `${f(r.weightKg, 2)} kg`;
    this.areaResult.textContent = `${f(r.areaM, 3)} m²`;
    this.volumeResult.textContent = `${f(r.volumeCm, 1)} cm³`;

    // 詳細計算過程(数値だけを入れる。HTML は組み立てない)
    const line = (label, expr, value, last = false) => {
      const div = document.createElement('div');
      if (!last) div.className = 'mb-2';
      div.append(`${label} = ${expr} = `);
      const span = document.createElement('span');
      span.className = 'value';
      span.textContent = value;
      div.append(span);
      return div;
    };
    this.areaDetailed.replaceChildren(
      line('面積(mm²)', `A × B = ${a} × ${b}`, `${f(r.areaMm, 0)} mm²`),
      line('面積(m²)', `面積(mm²) / 1,000,000 = ${f(r.areaMm, 0)} / 1,000,000`, `${f(r.areaM, 3)} m²`, true)
    );
    this.volumeDetailed.replaceChildren(
      line('体積(mm³)', `A × B × 板厚 = ${a} × ${b} × ${t.toFixed(2)}`, `${f(r.volumeMm, 0)} mm³`),
      line('体積(cm³)', `体積(mm³) / 1000 = ${f(r.volumeMm, 0)} / 1000`, `${f(r.volumeCm, 1)} cm³`, true)
    );
    this.weightDetailed.replaceChildren(
      line('重量(g)', `体積(cm³) × 比重(g/cm³) = ${f(r.volumeCm, 1)} × ${specificGravity.toFixed(2)}`, `${f(r.weightGrams, 1)} g`),
      line('重量(kg)', `重量(g) / 1000 = ${f(r.weightGrams, 1)} / 1000`, `${f(r.weightKg, 2)} kg`, true)
    );

    // モーダル内の詳細
    this.modalVolumeMm.textContent = `= ${a} × ${b} × ${t.toFixed(2)} = ${f(r.volumeMm, 0)} mm³`;
    this.modalVolumeCm.textContent = `= ${f(r.volumeMm, 0)} / 1000 = ${f(r.volumeCm, 1)} cm³`;
    this.modalWeightG.textContent = `= ${f(r.volumeCm, 1)} × ${specificGravity.toFixed(2)} = ${f(r.weightGrams, 1)} g`;
    this.modalWeightKg.textContent = `= ${f(r.weightGrams, 1)} / 1000 = ${f(r.weightKg, 2)} kg`;
    this.modalAreaMm.textContent = `= ${a} × ${b} = ${f(r.areaMm, 0)} mm²`;
    this.modalAreaM.textContent = `= ${f(r.areaMm, 0)} / 1,000,000 = ${f(r.areaM, 3)} m²`;
    this.modalVolumeMm2.textContent = `= ${f(r.areaMm, 0)} × ${t.toFixed(2)} = ${f(r.volumeMm, 0)} mm³`;
    this.modalVolumeCm2.textContent = `= ${f(r.volumeMm, 0)} / 1000 = ${f(r.volumeCm, 1)} cm³`;

    // 3D・グラフへ知らせる
    document.dispatchEvent(new CustomEvent('plate-calculated', { detail: { ...values, ...r } }));
  }
}

// アプリケーションを初期化
document.addEventListener('DOMContentLoaded', () => {
  new PlateCalculator();
});
