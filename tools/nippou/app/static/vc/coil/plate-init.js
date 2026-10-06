/*
  plate-init.js — 平板の3Dビューアの初期化とボタンの繋ぎ込み(coil-init.js と同じ仕組み)
  平板の画面は最初は隠れているので、初めて見せたとき(shape-shown)に3Dを作る
  (隠れたまま作ると大きさが 0 になるため)。
*/
(() => {
    const $ = (id) => document.getElementById(id);

    function currentSize() {
        return {
            a: parseFloat($('plate-a').value),
            b: parseFloat($('plate-b').value),
            t: parseFloat($('plate-t').value)
        };
    }

    function initializePlateViewer() {
        if (window.plateViewer !== undefined) return; // 既に初期化済み(または使えない)

        // WebGL が使えない PC では 3D だけ出さない。計算とグラフはそのまま使える
        try {
            window.plateViewer = new PlateViewer3D('plate-3d-container');
        } catch (err) {
            console.warn('3D表示を使えません:', err);
            window.plateViewer = null;
            const note = document.createElement('p');
            note.className = 'error-message';
            note.style.padding = '16px';
            note.textContent = 'このPCでは3D表示を使えません(WebGL が無効です)。計算とグラフは使えます。';
            $('plate-3d-container').replaceChildren(note);
            return;
        }

        const selected = $('plate-material-preset').selectedOptions[0];
        const color = $('plate-color').value !== '#aaaaaa' ? $('plate-color').value
            : (selected && selected.getAttribute('data-color')) || '#aaaaaa';
        window.plateViewer.setPlateColor(color);
        const s = currentSize();
        window.plateViewer.updatePlateModel(s.a, s.b, s.t);
    }

    document.addEventListener('DOMContentLoaded', () => {
        // 計算のたびに3Dを更新(範囲外の値のときは計算しないので、3Dもそのまま)
        document.addEventListener('plate-calculated', (e) => {
            if (window.plateViewer) {
                window.plateViewer.updatePlateModel(e.detail.a, e.detail.b, e.detail.t);
            }
        });

        document.addEventListener('plate-exaggerated', (e) => {
            $('plate-exaggerated-note').hidden = !e.detail.exaggerated;
        });

        // コントロールボタン
        $('plate-toggle-rotation').addEventListener('click', () => {
            if (window.plateViewer) window.plateViewer.setAutoRotate(!window.plateViewer.controls.autoRotate);
        });
        $('plate-reset-camera').addEventListener('click', () => {
            if (window.plateViewer) window.plateViewer.resetCamera();
        });
        $('plate-toggle-details').addEventListener('click', () => {
            if (window.plateViewer) window.plateViewer.toggleInfo();
        });

        // カラーピッカー
        $('plate-color').addEventListener('input', (e) => {
            if (window.plateViewer) window.plateViewer.setPlateColor(e.target.value);
        });
        $('plate-bg-color').addEventListener('input', (e) => {
            if (window.plateViewer) window.plateViewer.setBackgroundColor(e.target.value);
        });

        // 材質プリセット切替時に3Dの色も変える
        $('plate-material-preset').addEventListener('change', (e) => {
            const materialColor = e.target.selectedOptions[0].getAttribute('data-color');
            $('plate-color').value = materialColor;
            if (window.plateViewer) window.plateViewer.setPlateColor(materialColor);
        });

        // 平板の画面を見せたときに3Dを作る/大きさを合わせる
        document.addEventListener('shape-shown', (e) => {
            if (e.detail.shape !== 'plate') return;
            initializePlateViewer();
            if (window.plateViewer) window.plateViewer.onWindowResize();
        });
    });
})();
