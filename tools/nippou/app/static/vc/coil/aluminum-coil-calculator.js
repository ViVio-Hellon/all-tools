/**
 * アルミニウムコイル重量・長さ計算ツール
 * コイルの肉厚、内径、幅から重量、長さ、巻き数を計算するツール
 */

class CoilCalculator {
  /**
   * 計算機クラスを初期化
   */
  constructor() {
    // 定数
    this.PI = Math.PI;

    // DOM要素の参照を取得
    this.initializeInputElements();
    this.initializeErrorElements();
    this.initializeResultElements();
    this.initializeDetailedElements();
    this.initializeModalElements();

    // グローバル参照を追加
    window.coilCalculator = this;

    // イベントリスナーを設定
    this.setupEventListeners();

    // 初期計算を実行
    this.calculate();
  }

  /**
   * 入力フィールド要素の参照を取得
   */
  initializeInputElements() {
    this.thicknessInput = document.getElementById('thickness');
    this.innerDiameterInput = document.getElementById('inner-diameter');
    this.coilWidthInput = document.getElementById('coil-width');
    this.specificGravityInput = document.getElementById('specific-gravity');
    this.plateThicknessInput = document.getElementById('plate-thickness');
    this.materialPreset = document.getElementById('material-preset');
  }

  /**
   * エラーメッセージ要素の参照を取得
   */
  initializeErrorElements() {
    this.thicknessError = document.getElementById('thickness-error');
    this.innerDiameterError = document.getElementById('inner-diameter-error');
    this.coilWidthError = document.getElementById('coil-width-error');
    this.specificGravityError = document.getElementById('specific-gravity-error');
    this.plateThicknessError = document.getElementById('plate-thickness-error');
  }

  /**
   * 結果表示要素の参照を取得
   */
  initializeResultElements() {
    this.weightResult = document.getElementById('weight-result');
    this.lengthResult = document.getElementById('length-result');
    this.windingsResult = document.getElementById('windings-result');
  }

  /**
   * 詳細計算過程表示要素の参照を取得
   */
  initializeDetailedElements() {
    this.dimensionsDetailed = document.getElementById('dimensions-detailed');
    this.volumeDetailed = document.getElementById('volume-detailed');
    this.weightDetailed = document.getElementById('weight-detailed');
    this.lengthDetailed = document.getElementById('length-detailed');
    this.windingsDetailed = document.getElementById('windings-detailed');
  }

  /**
   * モーダル内の詳細表示要素の参照を取得
   */
  initializeModalElements() {
    this.modalLargeRadius = document.getElementById('modal-large-radius');
    this.modalSmallRadius = document.getElementById('modal-small-radius');
    this.modalLargeVolume = document.getElementById('modal-large-volume');
    this.modalSmallVolume = document.getElementById('modal-small-volume');
    this.modalCoilVolume = document.getElementById('modal-coil-volume');
    this.modalCoilVolumeCm = document.getElementById('modal-coil-volume-cm');
    this.modalWeightG = document.getElementById('modal-weight-g');
    this.modalWeightKg = document.getElementById('modal-weight-kg');
    this.modalCrossSection = document.getElementById('modal-cross-section');
    this.modalLengthArea = document.getElementById('modal-length-area');
    this.modalLengthMm = document.getElementById('modal-length-mm');
    this.modalLengthM = document.getElementById('modal-length-m');
    this.modalWindings = document.getElementById('modal-windings');
  }

  /**
   * 各種イベントリスナーを設定
   */
  setupEventListeners() {
    // タブ切り替え
    const tabs = document.querySelectorAll('.tab');
    const tabContents = document.querySelectorAll('.tab-content');

    tabs.forEach(tab => {
      tab.addEventListener('click', function() {
        const tabId = this.getAttribute('data-tab');
        
        // すべてのタブからactiveクラスを削除
        tabs.forEach(t => t.classList.remove('active'));
        // すべてのタブコンテンツからactiveクラスを削除
        tabContents.forEach(content => content.classList.remove('active'));
        
        // クリックしたタブとそれに対応するコンテンツにactiveクラスを追加
        this.classList.add('active');
        document.getElementById(tabId).classList.add('active');
      });
    });

    // モーダル管理
    const detailButtons = document.querySelectorAll('.details-btn');
    const modalCloseButtons = document.querySelectorAll('.modal-close');
    const modals = document.querySelectorAll('.modal');

    detailButtons.forEach(button => {
      button.addEventListener('click', function() {
        const modalId = this.getAttribute('data-modal');
        document.getElementById(modalId).classList.add('show');
      });
    });

    modalCloseButtons.forEach(button => {
      button.addEventListener('click', () => {
        const modal = button.closest('.modal');
        modal.classList.remove('show');
      });
    });

    // モーダルの外側をクリックした時にモーダルを閉じる
    modals.forEach(modal => {
      modal.addEventListener('click', function(event) {
        if (event.target === this) {
          this.classList.remove('show');
        }
      });
    });

    // 材質プリセットのイベントリスナー
    this.materialPreset.addEventListener('change', () => {
      const selectedOption = this.materialPreset.options[this.materialPreset.selectedIndex];
      this.specificGravityInput.value = selectedOption.getAttribute('data-gravity');
      
      // 計算を更新
      this.calculate();
    });

    // 入力フィールドのイベントリスナー
    this.thicknessInput.addEventListener('input', () => {
      if (this.validateInput(this.thicknessInput, 0, 600, this.thicknessError)) {
        this.calculate();
      }
    });

    this.innerDiameterInput.addEventListener('input', () => {
      if (this.validateInput(this.innerDiameterInput, 250, 610, this.innerDiameterError)) {
        this.calculate();
      }
    });

    this.coilWidthInput.addEventListener('input', () => {
      if (this.validateInput(this.coilWidthInput, 0, 1800, this.coilWidthError)) {
        this.calculate();
      }
    });

    this.specificGravityInput.addEventListener('input', () => {
      if (this.validateInput(this.specificGravityInput, 0.01, 100, this.specificGravityError)) {
        this.calculate();
      }
    });

    this.plateThicknessInput.addEventListener('input', () => {
      if (this.validateInput(this.plateThicknessInput, 0.05, 20, this.plateThicknessError)) {
        this.calculate();
      }
    });
  }

  /**
   * 入力値の検証を行う
   * @param {HTMLElement} input - 入力要素
   * @param {number} min - 最小値
   * @param {number} max - 最大値
   * @param {HTMLElement} errorElement - エラー表示要素
   * @return {boolean} 検証結果
   */
  validateInput(input, min, max, errorElement) {
    const value = parseFloat(input.value);
    if (isNaN(value) || value < min || value > max) {
      input.classList.add('error');
      errorElement.style.display = 'block';
      return false;
    } else {
      input.classList.remove('error');
      errorElement.style.display = 'none';
      return true;
    }
  }

  /**
   * 数値をカンマ区切りで表示する
   * @param {number} number - 整形する数値
   * @param {number} decimals - 小数点以下の桁数
   * @return {string} カンマ区切りされた数値文字列
   */
  formatNumber(number, decimals = 1) {
    return number.toFixed(decimals).replace(/\B(?=(\d{3})+(?!\d))/g, ",");
  }

  /**
   * 計算を実行して表示を更新する
   */
  calculate() {
    // 入力値を取得
    const thickness = parseFloat(this.thicknessInput.value);
    const innerDiameter = parseFloat(this.innerDiameterInput.value);
    const coilWidth = parseFloat(this.coilWidthInput.value);
    const specificGravity = parseFloat(this.specificGravityInput.value);
    const plateThickness = parseFloat(this.plateThicknessInput.value);

    // 基本寸法計算
    const largeRadius = (thickness * 2 + innerDiameter) / 2;
    const smallRadius = innerDiameter / 2;

    // 体積計算
    const largeVolume = this.PI * Math.pow(largeRadius, 2) * coilWidth;
    const smallVolume = this.PI * Math.pow(smallRadius, 2) * coilWidth;
    const coilVolume = largeVolume - smallVolume;
    const coilVolumeCm = coilVolume / 1000;

    // 重量計算
    const weightGrams = coilVolumeCm * specificGravity;
    const weightKg = weightGrams / 1000;

    // 長さ計算
    const crossSection = this.PI * (Math.pow(largeRadius, 2) - Math.pow(smallRadius, 2));
    const lengthArea = coilVolume / plateThickness; // 断面積×長さ = mm²
    const lengthMm = lengthArea / coilWidth; // mm²/mm = mm
    const lengthM = lengthMm / 1000; // mm/1000 = m

    // 巻き数計算
    const windingCount = thickness / plateThickness;
    const roundedWindingCount = Math.round(windingCount);

    // 結果値を更新
    this.weightResult.textContent = `${this.formatNumber(weightKg)} kg`;
    this.lengthResult.textContent = `${this.formatNumber(lengthM)} m`;
    this.windingsResult.textContent = `${roundedWindingCount} 巻`;

    // 詳細計算過程を更新
    this.updateDetailedCalculations(thickness, innerDiameter, coilWidth, specificGravity, plateThickness, 
                              largeRadius, smallRadius, largeVolume, smallVolume, coilVolume, coilVolumeCm, 
                              weightGrams, weightKg, crossSection, lengthArea, lengthMm, lengthM, windingCount, roundedWindingCount);
  }

  /**
   * 詳細計算過程の表示を更新
   * @param {Object} params - 計算に使用するパラメータ一式
   */
  updateDetailedCalculations(thickness, innerDiameter, coilWidth, specificGravity, plateThickness, 
                           largeRadius, smallRadius, largeVolume, smallVolume, coilVolume, coilVolumeCm, 
                           weightGrams, weightKg, crossSection, lengthArea, lengthMm, lengthM, windingCount, roundedWindingCount) {
    
    // 詳細計算過程を更新
    this.dimensionsDetailed.innerHTML = `
      <div class="mb-2">大円柱半径 = (肉厚 × 2 + 内径) / 2 = (${thickness} × 2 + ${innerDiameter}) / 2 = <span class="value">${this.formatNumber(largeRadius)} mm</span></div>
      <div>小円柱半径 = 内径 / 2 = ${innerDiameter} / 2 = <span class="value">${this.formatNumber(smallRadius)} mm</span></div>
    `;

    this.volumeDetailed.innerHTML = `
      <div class="mb-2">大円柱体積 = π × 大円柱半径² × コイル幅 = π × ${this.formatNumber(largeRadius)}² × ${coilWidth} = <span class="value">${this.formatNumber(largeVolume, 0)} mm³</span></div>
      <div class="mb-2">小円柱体積 = π × 小円柱半径² × コイル幅 = π × ${this.formatNumber(smallRadius)}² × ${coilWidth} = <span class="value">${this.formatNumber(smallVolume, 0)} mm³</span></div>
      <div class="mb-2">コイル体積 = 大円柱体積 - 小円柱体積 = ${this.formatNumber(largeVolume, 0)} - ${this.formatNumber(smallVolume, 0)} = <span class="value">${this.formatNumber(coilVolume, 0)} mm³</span></div>
      <div>コイル体積(cm³) = コイル体積(mm³) / 1000 = ${this.formatNumber(coilVolume, 0)} / 1000 = <span class="value">${this.formatNumber(coilVolumeCm)} cm³</span></div>
    `;

    this.weightDetailed.innerHTML = `
      <div class="mb-2">重量(g) = 体積(cm³) × 比重(g/cm³) = ${this.formatNumber(coilVolumeCm)} × ${specificGravity.toFixed(2)} = <span class="value">${this.formatNumber(weightGrams)} g</span></div>
      <div>重量(kg) = 重量(g) / 1000 = ${this.formatNumber(weightGrams)} / 1000 = <span class="value">${this.formatNumber(weightKg)} kg</span></div>
    `;

    this.lengthDetailed.innerHTML = `
      <div class="mb-2">断面積 = π × (大円柱半径² - 小円柱半径²) = π × (${this.formatNumber(largeRadius)}² - ${this.formatNumber(smallRadius)}²) = <span class="value">${this.formatNumber(crossSection)} mm²</span></div>
      <div class="mb-2">面積 = コイル体積(mm³) / 板厚(mm) = ${this.formatNumber(coilVolume, 0)} / ${plateThickness.toFixed(2)} = <span class="value">${this.formatNumber(lengthArea, 0)} mm²</span></div>
      <div class="mb-2">長さ(mm) = 面積(mm²) / コイル幅(mm) = ${this.formatNumber(lengthArea, 0)} / ${coilWidth} = <span class="value">${this.formatNumber(lengthMm, 0)} mm</span></div>
      <div>長さ(m) = 長さ(mm) / 1000 = ${this.formatNumber(lengthMm, 0)} / 1000 = <span class="value">${this.formatNumber(lengthM)} m</span></div>
    `;

    this.windingsDetailed.innerHTML = `
      <div>巻き数 = 肉厚(mm) / 板厚(mm) = ${thickness} / ${plateThickness.toFixed(2)} = <span class="value">${this.formatNumber(windingCount, 1)} ≈ ${roundedWindingCount} 巻</span></div>
    `;

    // モーダル内の詳細を更新
    this.modalLargeRadius.textContent = `= (${thickness} × 2 + ${innerDiameter}) / 2 = ${this.formatNumber(largeRadius)} mm`;
    this.modalSmallRadius.textContent = `= ${innerDiameter} / 2 = ${this.formatNumber(smallRadius)} mm`;
    this.modalLargeVolume.textContent = `= π × ${this.formatNumber(largeRadius)}² × ${coilWidth} = ${this.formatNumber(largeVolume, 0)} mm³`;
    this.modalSmallVolume.textContent = `= π × ${this.formatNumber(smallRadius)}² × ${coilWidth} = ${this.formatNumber(smallVolume, 0)} mm³`;
    this.modalCoilVolume.textContent = `= ${this.formatNumber(largeVolume, 0)} - ${this.formatNumber(smallVolume, 0)} = ${this.formatNumber(coilVolume, 0)} mm³`;
    this.modalCoilVolumeCm.textContent = `= ${this.formatNumber(coilVolume, 0)} / 1000 = ${this.formatNumber(coilVolumeCm)} cm³`;
    this.modalWeightG.textContent = `= ${this.formatNumber(coilVolumeCm)} × ${specificGravity.toFixed(2)} = ${this.formatNumber(weightGrams)} g`;
    this.modalWeightKg.textContent = `= ${this.formatNumber(weightGrams)} / 1000 = ${this.formatNumber(weightKg)} kg`;
    this.modalCrossSection.textContent = `= π × (${this.formatNumber(largeRadius)}² - ${this.formatNumber(smallRadius)}²) = ${this.formatNumber(crossSection)} mm²`;
    this.modalLengthArea.textContent = `= ${this.formatNumber(coilVolume, 0)} / ${plateThickness.toFixed(2)} = ${this.formatNumber(lengthArea, 0)} mm²`;
    this.modalLengthMm.textContent = `= ${this.formatNumber(lengthArea, 0)} / ${coilWidth} = ${this.formatNumber(lengthMm, 0)} mm`;
    this.modalLengthM.textContent = `= ${this.formatNumber(lengthMm, 0)} / 1000 = ${this.formatNumber(lengthM)} m`;
    this.modalWindings.textContent = `= ${thickness} / ${plateThickness.toFixed(2)} = ${this.formatNumber(windingCount, 1)} ≈ ${roundedWindingCount} 巻`;
  }
}

// アプリケーションを初期化
document.addEventListener('DOMContentLoaded', () => {
  new CoilCalculator();
});