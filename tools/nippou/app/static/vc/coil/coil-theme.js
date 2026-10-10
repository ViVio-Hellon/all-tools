/*
  coil-theme.js — 背景をライト / ダークから選ぶ

  - 選んだ方はこの PC のブラウザに覚えておく(覚えられなくても動く)。
    まだ選んでいなければライト(日報・日報複合ツールの4ツールとそろえる)。
  - <head> で読む: 本文を描く前に data-theme を決めて、白く光ってから暗くなるのを防ぐ。
  - 色そのものは CSS(coil-theme.css)。ここは「どちらか」を決めて、CSS の届かない
    ところ(3D の背景・グラフの文字と罫線)を塗り直すだけ。
*/
(() => {
    const KEY = 'vc-calculator.coil.theme';
    const root = document.documentElement;

    // 3D の背景の既定(CSS の --well と同じ色)。利用者が色を選んでいたら触らない
    const WELL = { light: '#f5f5f5', dark: '#1d282e' };
    // グラフの文字と罫線
    const CHART = {
        light: { text: '#666666', grid: 'rgba(0, 0, 0, 0.05)' },
        dark: { text: '#c9d4da', grid: 'rgba(255, 255, 255, 0.08)' },
    };

    function saved() {
        try {
            const value = localStorage.getItem(KEY);
            return value === 'dark' || value === 'light' ? value : null;
        } catch (err) {
            return null;
        }
    }

    // 選んでいなければライト(日報・日報複合ツールの4ツールとそろえる)
    const DEFAULT = 'light';

    function current() {
        return root.dataset.theme === 'dark' ? 'dark' : 'light';
    }

    // --- 3D の背景 ---
    function paint3d(theme) {
        const other = theme === 'dark' ? 'light' : 'dark';
        const pairs = [['bg-color', window.coilViewer], ['plate-bg-color', window.plateViewer]];
        for (const [id, viewer] of pairs) {
            const input = document.getElementById(id);
            if (!input) continue;
            const value = input.value.toLowerCase();
            if (value === WELL[other]) input.value = WELL[theme];
            if (viewer && typeof viewer.setBackgroundColor === 'function'
                && input.value.toLowerCase() === WELL[theme]) {
                viewer.setBackgroundColor(input.value);
            }
        }
    }

    // --- グラフ(Chart.js)。作り直されても効くよう、描くたびに色を入れる ---
    if (typeof Chart !== 'undefined' && typeof Chart.register === 'function') {
        Chart.register({
            id: 'coilTheme',
            beforeUpdate(chart) {
                const colors = CHART[current()];
                for (const scale of Object.values(chart.options.scales || {})) {
                    if (scale.grid) scale.grid.color = colors.grid;
                    if (scale.ticks) scale.ticks.color = colors.text;
                    if (scale.title) scale.title.color = colors.text;
                }
            },
        });
    }

    function paintCharts() {
        if (typeof Chart === 'undefined' || !Chart.instances) return;
        for (const chart of Object.values(Chart.instances)) chart.update('none');
    }

    function apply(theme, remember) {
        root.dataset.theme = theme;
        if (remember) {
            try { localStorage.setItem(KEY, theme); } catch (err) { /* 覚えられなくてもよい */ }
        }
        document.querySelectorAll('.theme-btn').forEach((btn) => {
            btn.setAttribute('aria-pressed', btn.dataset.themeChoice === theme ? 'true' : 'false');
        });
        paint3d(theme);
        paintCharts();
    }

    // 本文より先に決める(ちらつかせない)
    root.dataset.theme = saved() || DEFAULT;

    document.addEventListener('DOMContentLoaded', () => {
        document.querySelectorAll('.theme-btn').forEach((btn) => {
            btn.addEventListener('click', () => apply(btn.dataset.themeChoice, true));
        });
        apply(current(), false);
    });
    // 3D とグラフは DOMContentLoaded の中で作られるので、出そろってからもう一度塗る
    window.addEventListener('load', () => apply(current(), false));
    // 平板の 3D は初めて見せたとき(shape-shown)に作られる。ここは先に読まれているので、
    // 作られる処理より先に呼ばれる ── 作り終わってから塗る
    document.addEventListener('shape-shown', () => setTimeout(() => paint3d(current()), 0));

})();
