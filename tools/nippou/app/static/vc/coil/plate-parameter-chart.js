/**
 * 平板パラメータグラフ
 * 入力パラメータと計算結果の関係を視覚化する(coil-parameter-chart.js と同じ仕組み)
 */
class PlateParameterChart {
    constructor() {
        this.chartCanvas = document.getElementById('plate-parameter-chart');
        if (!this.chartCanvas) {
            console.error('Chart canvas element not found');
            return;
        }

        this.xAxisSelect = document.getElementById('plate-x-axis-param');
        this.yAxisSelect = document.getElementById('plate-y-axis-result');

        // 入力パラメータの範囲(画面の ※ と同じ)
        const ranges = window.plateCalculator.ranges;
        this.paramRanges = {
            'a': { ...ranges.a, unit: 'mm', label: 'A' },
            'b': { ...ranges.b, unit: 'mm', label: 'B' },
            't': { ...ranges.t, unit: 'mm', label: '板厚' }
        };

        // 結果タイプ
        this.resultTypes = {
            'weight': { unit: 'kg', label: '重量', color: 'rgba(66, 133, 244, 0.7)', key: 'weightKg' },
            'area': { unit: 'm²', label: '面積', color: 'rgba(15, 157, 88, 0.7)', key: 'areaM' },
            'volume': { unit: 'cm³', label: '体積', color: 'rgba(219, 68, 55, 0.7)', key: 'volumeCm' }
        };

        this.selectedXParam = this.xAxisSelect.value;
        this.selectedYResult = this.yAxisSelect.value;
        this.chart = null;

        this.setupEventListeners();
        this.initializeChart();
        this.updateChart();
    }

    setupEventListeners() {
        this.xAxisSelect.addEventListener('change', () => {
            this.selectedXParam = this.xAxisSelect.value;
            this.updateChart();
        });
        this.yAxisSelect.addEventListener('change', () => {
            this.selectedYResult = this.yAxisSelect.value;
            this.updateChart();
        });
        // 計算のたびに線と現在値を描き直す(ほかの入力が変われば線の形も変わるため)
        document.addEventListener('plate-calculated', () => this.updateChart());
    }

    initializeChart() {
        if (typeof Chart === 'undefined') {
            console.error('Chart.js library is not loaded!');
            return;
        }
        const ctx = this.chartCanvas.getContext('2d');
        this.chart = new Chart(ctx, {
            type: 'line',
            data: {
                labels: [],
                datasets: [{
                    label: '',
                    data: [],
                    borderWidth: 2,
                    tension: 0,
                    fill: true,
                    pointRadius: 0
                }]
            },
            options: {
                responsive: true,
                maintainAspectRatio: false,
                parsing: false,
                plugins: {
                    tooltip: {
                        mode: 'index',
                        intersect: false,
                        callbacks: {
                            title: (items) => {
                                const p = this.paramRanges[this.selectedXParam];
                                return `${p.label}: ${this.formatX(items[0].parsed.x)} ${p.unit}`;
                            },
                            label: (item) => {
                                const r = this.resultTypes[this.selectedYResult];
                                return `${r.label}: ${this.formatY(item.parsed.y)} ${r.unit}`;
                            }
                        }
                    },
                    legend: { display: false },
                    annotation: { annotations: {} }
                },
                scales: {
                    x: {
                        type: 'linear',
                        title: { display: true, text: '' },
                        grid: { color: 'rgba(0, 0, 0, 0.05)' },
                        ticks: { callback: (value) => this.formatX(value), maxTicksLimit: 10 }
                    },
                    y: {
                        title: { display: true, text: '' },
                        grid: { color: 'rgba(0, 0, 0, 0.05)' },
                        beginAtZero: true
                    }
                },
                interaction: { mode: 'index', intersect: false }
            }
        });
    }

    formatX(value) {
        return this.selectedXParam === 't'
            ? (Number.isInteger(value) ? value : Number(value).toFixed(2))
            : Math.round(value);
    }

    formatY(value) {
        const digits = { weight: 2, area: 3, volume: 1 }[this.selectedYResult];
        return Number(value).toFixed(digits);
    }

    /**
     * いまの入力を元に、X軸のパラメータだけを動かして計算する
     */
    generateChartData(current) {
        const range = this.paramRanges[this.selectedXParam];
        const resultKey = this.resultTypes[this.selectedYResult].key;
        const steps = 20;
        const stepSize = (range.max - range.min) / steps;
        const data = [];
        for (let i = 0; i <= steps; i++) {
            const x = range.min + stepSize * i;
            const v = { ...current, [this.selectedXParam]: x };
            const r = PlateCalculator.compute(v.a, v.b, v.t, v.specificGravity);
            data.push({ x, y: r[resultKey] });
        }
        return data;
    }

    updateChart() {
        if (!this.chart) return;
        const current = window.plateCalculator.currentValues();
        if (!current) return; // 範囲外の値があるあいだは描き直さない

        const xInfo = this.paramRanges[this.selectedXParam];
        const yInfo = this.resultTypes[this.selectedYResult];
        const options = this.chart.options;
        options.scales.x.title.text = `${xInfo.label} (${xInfo.unit})`;
        options.scales.y.title.text = `${yInfo.label} (${yInfo.unit})`;
        options.scales.x.min = xInfo.min;
        options.scales.x.max = xInfo.max;

        const dataset = this.chart.data.datasets[0];
        dataset.data = this.generateChartData(current);
        dataset.label = yInfo.label;
        dataset.borderColor = yInfo.color;
        dataset.backgroundColor = yInfo.color.replace('0.7', '0.1');

        // 現在値のマーカー
        options.plugins.annotation.annotations = {
            currentValue: {
                type: 'line',
                scaleID: 'x',
                value: current[this.selectedXParam],
                borderColor: 'rgba(255, 99, 132, 0.8)',
                borderWidth: 2,
                borderDash: [5, 5],
                label: {
                    display: true,
                    content: '現在値',
                    position: 'start',
                    backgroundColor: 'rgba(255, 99, 132, 0.8)',
                    font: { size: 12, weight: 'bold' }
                }
            }
        };
        this.chart.update();
    }
}

document.addEventListener('DOMContentLoaded', () => {
    if (typeof Chart === 'undefined') {
        // Chart.js はアプリに同梱している(app/static/vc/vendor/chartjs/)。インターネットからは読まない
        console.error('Chart.js library is not loaded.');
        return;
    }
    window.plateParameterChart = new PlateParameterChart();
});
