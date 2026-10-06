/**
 * アルミニウムコイルパラメータグラフ
 * 入力パラメータと計算結果の関係を視覚化するグラフ機能
 */
class CoilParameterChart {
    /**
     * グラフ機能の初期化
     */
    constructor() {
        // チャート要素の参照
        this.chartCanvas = document.getElementById('parameter-chart');
        if (!this.chartCanvas) {
            console.error('Chart canvas element not found');
            return;
        }

        // 入力および結果要素の参照
        this.initializeElements();
        
        // 入力パラメータの範囲定義
        this.paramRanges = {
            'thickness': { min: 0, max: 600, step: 30, unit: 'mm', label: '肉厚' },
            'inner-diameter': { min: 250, max: 610, step: 18, unit: 'mm', label: '内径' },
            'coil-width': { min: 0, max: 1800, step: 90, unit: 'mm', label: 'コイル幅' },
            'plate-thickness': { min: 0.05, max: 20, step: 1, unit: 'mm', label: '板厚' }
        };
        
        // 結果タイプの定義
        this.resultTypes = {
            'weight': { unit: 'kg', label: '重量', color: 'rgba(66, 133, 244, 0.7)' },
            'length': { unit: 'm', label: '長さ', color: 'rgba(15, 157, 88, 0.7)' },
            'windings': { unit: '巻', label: '巻き数', color: 'rgba(219, 68, 55, 0.7)' }
        };
        
        // デフォルト選択
        this.selectedXParam = 'thickness';
        this.selectedYResult = 'weight';
        
        // Chart.js インスタンス
        this.chart = null;
        
        // 計算機インスタンス (window.coilCalculatorに格納されていることを想定)
        this.calculator = window.coilCalculator;
        
        // イベントリスナーの設定
        this.setupEventListeners();
        
        // 初期グラフの描画
        this.initializeChart();
        this.updateChart();
    }
    
    /**
     * 要素の参照を初期化
     */
    initializeElements() {
        // 軸選択要素
        this.xAxisSelect = document.getElementById('x-axis-param');
        this.yAxisSelect = document.getElementById('y-axis-result');
        
        // 入力フィールド要素
        this.thicknessInput = document.getElementById('thickness');
        this.innerDiameterInput = document.getElementById('inner-diameter');
        this.coilWidthInput = document.getElementById('coil-width');
        this.specificGravityInput = document.getElementById('specific-gravity');
        this.plateThicknessInput = document.getElementById('plate-thickness');
    }
    
    /**
     * イベントリスナーを設定
     */
    setupEventListeners() {
        // 軸パラメータ選択変更時
        this.xAxisSelect.addEventListener('change', () => {
            this.selectedXParam = this.xAxisSelect.value;
            this.updateChart();
        });
        
        this.yAxisSelect.addEventListener('change', () => {
            this.selectedYResult = this.yAxisSelect.value;
            this.updateChart();
        });
        
        // 入力値変更時のグラフ更新
        const inputs = [
            this.thicknessInput,
            this.innerDiameterInput,
            this.coilWidthInput,
            this.specificGravityInput,
            this.plateThicknessInput
        ];
        
        inputs.forEach(input => {
            if (input) {
                input.addEventListener('input', () => {
                    // 値が有効な場合のみグラフのマーカーを更新
                    if (!input.classList.contains('error')) {
                        this.updateChartMarker();
                    }
                });
            }
        });
    }
    
    /**
     * グラフ初期化
     */
    initializeChart() {
        // Chart.jsが読み込まれているか確認
        if (typeof Chart === 'undefined') {
            console.error('Chart.js library is not loaded!');
            return;
        }
        
        const ctx = this.chartCanvas.getContext('2d');
        
        // 現在選択されているパラメータの範囲を取得
        const paramRange = this.paramRanges[this.selectedXParam];
        
        // グラフ設定
        this.chart = new Chart(ctx, {
            type: 'line',
            data: {
                labels: [],
                datasets: [{
                    label: '',
                    data: [],
                    borderColor: this.resultTypes[this.selectedYResult].color,
                    backgroundColor: this.resultTypes[this.selectedYResult].color.replace('0.7', '0.1'),
                    borderWidth: 2,
                    tension: 0.2,
                    fill: true,
                    pointRadius: 0
                }]
            },
            options: {
                responsive: true,
                maintainAspectRatio: false,
                plugins: {
                    tooltip: {
                        mode: 'index',
                        intersect: false,
                        callbacks: {
                            title: (tooltipItems) => {
                                const xValue = tooltipItems[0].parsed.x;
                                const paramInfo = this.paramRanges[this.selectedXParam];
                                return `${paramInfo.label}: ${xValue} ${paramInfo.unit}`;
                            },
                            label: (tooltipItem) => {
                                const resultInfo = this.resultTypes[this.selectedYResult];
                                return `${resultInfo.label}: ${tooltipItem.parsed.y.toFixed(1)} ${resultInfo.unit}`;
                            }
                        }
                    },
                    legend: {
                        display: false
                    },
                    annotation: {
                        annotations: {
                            currentValue: {
                                type: 'line',
                                xMin: 0,
                                xMax: 0,
                                borderColor: 'rgba(255, 99, 132, 0.8)',
                                borderWidth: 2,
                                borderDash: [5, 5],
                                label: {
                                    display: true,
                                    content: '現在値',
                                    position: 'start'
                                }
                            }
                        }
                    }
                },
                scales: {
                    x: {
                        type: 'linear',
                        min: paramRange.min,
                        max: paramRange.max,
                        title: {
                            display: true,
                            text: this.paramRanges[this.selectedXParam].label + ' (' + this.paramRanges[this.selectedXParam].unit + ')'
                        },
                        grid: {
                            color: 'rgba(0, 0, 0, 0.05)'
                        },
                        ticks: {
                            callback: (value) => {
                                // パラメータタイプに応じてフォーマットを変更
                                if (this.selectedXParam === 'plate-thickness') {
                                    // 板厚の場合のみ小数点以下最大2桁表示
                                    return Number.isInteger(value) ? value : value.toFixed(2);
                                } else {
                                    // その他のパラメータ（肉厚、内径、コイル幅）は整数表示
                                    return Math.round(value);
                                }
                            },
                            // 目盛りの数を制限して表示を見やすくする
                            maxTicksLimit: 10
                        }
                    },
                    y: {
                        title: {
                            display: true,
                            text: this.resultTypes[this.selectedYResult].label + ' (' + this.resultTypes[this.selectedYResult].unit + ')'
                        },
                        grid: {
                            color: 'rgba(0, 0, 0, 0.05)'
                        },
                        beginAtZero: true
                    }
                },
                interaction: {
                    mode: 'index',
                    intersect: false
                }
            }
        });
    }
    
    /**
     * グラフデータを生成
     * @returns {Object} グラフデータ
     */
    generateChartData() {
        // X軸パラメータの範囲と刻み値
        const paramRange = this.paramRanges[this.selectedXParam];
        const steps = 20; // データポイント数
        const stepSize = (paramRange.max - paramRange.min) / steps;
        
        // 現在の入力値を取得
        const currentValues = {
            thickness: parseFloat(this.thicknessInput.value),
            innerDiameter: parseFloat(this.innerDiameterInput.value),
            coilWidth: parseFloat(this.coilWidthInput.value),
            specificGravity: parseFloat(this.specificGravityInput.value),
            plateThickness: parseFloat(this.plateThicknessInput.value)
        };
        
        // ラベルとデータ配列を初期化
        const labels = [];
        const data = [];
        
        // 各ステップでの計算結果を取得
        for (let i = 0; i <= steps; i++) {
            const paramValue = paramRange.min + (stepSize * i);
            labels.push(paramValue);
            
            // 計算用に現在の値をコピー
            const calcValues = { ...currentValues };
            
            // X軸のパラメータ値を更新
            switch (this.selectedXParam) {
                case 'thickness':
                    calcValues.thickness = paramValue;
                    break;
                case 'inner-diameter':
                    calcValues.innerDiameter = paramValue;
                    break;
                case 'coil-width':
                    calcValues.coilWidth = paramValue;
                    break;
                case 'plate-thickness':
                    calcValues.plateThickness = paramValue;
                    break;
            }
            
            // パラメータを使って計算
            const result = this.calculateResult(
                calcValues.thickness,
                calcValues.innerDiameter,
                calcValues.coilWidth,
                calcValues.specificGravity,
                calcValues.plateThickness
            );
            
            // 選択された結果タイプに応じたデータを追加
            data.push(result[this.selectedYResult]);
        }
        
        return { labels, data };
    }
    
    /**
     * 計算実行（calculator メソッドのラッパー）
     * @param {number} thickness - 肉厚(mm)
     * @param {number} innerDiameter - 内径(mm)
     * @param {number} coilWidth - コイル幅(mm)
     * @param {number} specificGravity - 比重(g/cm³)
     * @param {number} plateThickness - 板厚(mm)
     * @returns {Object} 計算結果オブジェクト
     */
    calculateResult(thickness, innerDiameter, coilWidth, specificGravity, plateThickness) {
        // 基本寸法計算
        const largeRadius = (thickness * 2 + innerDiameter) / 2;
        const smallRadius = innerDiameter / 2;

        // 体積計算
        const PI = Math.PI;
        const largeVolume = PI * Math.pow(largeRadius, 2) * coilWidth;
        const smallVolume = PI * Math.pow(smallRadius, 2) * coilWidth;
        const coilVolume = largeVolume - smallVolume;
        const coilVolumeCm = coilVolume / 1000;

        // 重量計算
        const weightGrams = coilVolumeCm * specificGravity;
        const weightKg = weightGrams / 1000;

        // 長さ計算
        const lengthArea = coilVolume / plateThickness;
        const lengthMm = lengthArea / coilWidth;
        const lengthM = lengthMm / 1000;

        // 巻き数計算
        const windingCount = thickness / plateThickness;
        const roundedWindingCount = Math.round(windingCount);

        return {
            weight: weightKg,
            length: lengthM,
            windings: roundedWindingCount
        };
    }
    
    /**
     * グラフを更新
     */
    updateChart() {
        // チャートが初期化されているか確認
        if (!this.chart) {
            console.warn('Chart is not initialized yet.');
            return;
        }
        
        // グラフデータを生成
        const { labels, data } = this.generateChartData();
        
        // グラフタイトルを更新
        const xParamInfo = this.paramRanges[this.selectedXParam];
        const yResultInfo = this.resultTypes[this.selectedYResult];
        
        // スケールタイトルを更新
        this.chart.options.scales.x.title.text = xParamInfo.label + ' (' + xParamInfo.unit + ')';
        this.chart.options.scales.y.title.text = yResultInfo.label + ' (' + yResultInfo.unit + ')';
        
        // X軸の範囲を選択されたパラメータに合わせて設定
        this.chart.options.scales.x.min = xParamInfo.min;
        this.chart.options.scales.x.max = xParamInfo.max;
        
        // X軸の目盛り表示を更新
        // パラメータタイプに応じたフォーマットでTicksを表示
        this.chart.options.scales.x.ticks.callback = (value) => {
            if (this.selectedXParam === 'plate-thickness') {
                // 板厚の場合のみ小数点以下最大2桁表示
                return Number.isInteger(value) ? value : value.toFixed(2);
            } else {
                // その他のパラメータ（肉厚、内径、コイル幅）は整数表示
                return Math.round(value);
            }
        };
        
        // データセット更新
        this.chart.data.labels = labels;
        this.chart.data.datasets[0].data = data;
        this.chart.data.datasets[0].label = yResultInfo.label;
        this.chart.data.datasets[0].borderColor = yResultInfo.color;
        this.chart.data.datasets[0].backgroundColor = yResultInfo.color.replace('0.7', '0.1');
        
        // 現在値マーカーを更新
        this.updateChartMarker();
        
        // グラフ再描画
        this.chart.update();
    }
    
    /**
     * 現在値マーカーを更新
     */
    updateChartMarker() {
        // 現在の入力値を取得
        let currentValue;
        
        switch (this.selectedXParam) {
            case 'thickness':
                currentValue = parseFloat(this.thicknessInput.value);
                break;
            case 'inner-diameter':
                currentValue = parseFloat(this.innerDiameterInput.value);
                break;
            case 'coil-width':
                currentValue = parseFloat(this.coilWidthInput.value);
                break;
            case 'plate-thickness':
                currentValue = parseFloat(this.plateThicknessInput.value);
                break;
        }
        
        // 無効な値の場合は更新をスキップ
        if (isNaN(currentValue)) {
            console.warn('現在値が無効です:', currentValue);
            return;
        }
        
        // アノテーションプラグインが利用可能か確認
        if (!this.chart.options.plugins) {
            this.chart.options.plugins = {};
        }
        
        if (!this.chart.options.plugins.annotation) {
            this.chart.options.plugins.annotation = {};
        }
        
        if (!this.chart.options.plugins.annotation.annotations) {
            this.chart.options.plugins.annotation.annotations = {};
        }
        
        // 現在値のラインアノテーションを設定
        this.chart.options.plugins.annotation.annotations.currentValue = {
            type: 'line',
            scaleID: 'x',
            value: currentValue,
            borderColor: 'rgba(255, 99, 132, 0.8)',
            borderWidth: 2,
            borderDash: [5, 5],
            label: {
                display: true,
                content: '現在値',
                position: 'top',
                backgroundColor: 'rgba(255, 99, 132, 0.8)',
                font: {
                    size: 12,
                    weight: 'bold'
                }
            }
        };
        
        // アノテーションの値をコンソールに表示（デバッグ用）
        // console.log(`マーカー更新: ${this.selectedXParam} = ${currentValue}`);
        
        // グラフ更新
        if (this.chart) {
            this.chart.update();
        }
    }
}

// ページ読み込み時にグラフ機能を初期化
document.addEventListener('DOMContentLoaded', () => {
    // Chart.jsが読み込まれたか確認
    if (typeof Chart === 'undefined') {
        // Chart.js はアプリに同梱している(app/static/vc/vendor/chartjs/)。インターネットからは
        // 読み込まない(ラインPCは外に出られない・アプリの CSP も外のスクリプトを断る)
        console.error('Chart.js library is not loaded.');
    } else {
        // Chart.jsが既に読み込まれている場合は直接初期化
        initializeCoilChart();
    }
});

// グラフ初期化関数
function initializeCoilChart() {
    // 計算機インスタンスが読み込まれるまで少し待つ
    setTimeout(() => {
        if (typeof window.coilCalculator !== 'undefined') {
            window.coilParameterChart = new CoilParameterChart();
        } else {
            // まだ計算機が読み込まれていない場合は再試行
            console.log('Waiting for coilCalculator to be initialized...');
            setTimeout(() => {
                window.coilParameterChart = new CoilParameterChart();
            }, 1000);
        }
    }, 500);
}