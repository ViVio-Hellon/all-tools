/*
  shape-switch.js — 形状(コイル / 平板)の切替
  選んだ形状はこの PC のブラウザに覚えておく(覚えられなくても動く)。
  見せたときに `shape-shown` を出し、3D の大きさを合わせる。
*/
(() => {
    const KEY = 'vc-calculator.coil.shape';

    function show(shape) {
        document.querySelectorAll('.shape-btn').forEach(btn => {
            const on = btn.getAttribute('data-shape') === shape;
            btn.classList.toggle('active', on);
            btn.setAttribute('aria-selected', on ? 'true' : 'false');
        });
        document.getElementById('coil-section').hidden = shape !== 'coil';
        document.getElementById('plate-section').hidden = shape !== 'plate';
        document.title = shape === 'plate' ? '平板重量計算ツール' : 'アルミニウムコイル重量・長さ計算ツール';
        try { localStorage.setItem(KEY, shape); } catch (err) { /* 覚えられなくてもよい */ }
        document.dispatchEvent(new CustomEvent('shape-shown', { detail: { shape } }));
        // 隠れているあいだに窓の大きさが変わっていたら 3D・グラフを合わせ直す
        window.dispatchEvent(new Event('resize'));
    }

    document.addEventListener('DOMContentLoaded', () => {
        document.querySelectorAll('.shape-btn').forEach(btn => {
            btn.addEventListener('click', () => show(btn.getAttribute('data-shape')));
        });
        let saved = 'coil';
        try { saved = localStorage.getItem(KEY) === 'plate' ? 'plate' : 'coil'; } catch (err) { /* 既定はコイル */ }
        if (saved === 'plate') show('plate');
    });
})();
