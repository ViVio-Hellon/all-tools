/*
  coil-init.js — 3Dビューアの初期化とボタンの繋ぎ込み
  (元は index.html の中に直接書いてあったもの。アプリの CSP は script-src 'self' で
  HTML の中のスクリプトを動かさないため、中身はそのままこのファイルへ移した)
*/
// 3Dビューアの初期化
document.addEventListener('DOMContentLoaded', () => {
    // 3Dビューアを初期化
    function initializeCoilViewer() {
        if (window.coilViewer) return; // 既に初期化済みなら何もしない

        // WebGL が使えない PC(グラフィックの機能が止められているなど)では 3D だけ出さない。
        // 計算とグラフはそのまま使える
        try {
            window.coilViewer = new CoilViewer3D('3d-container');
        } catch (err) {
            console.warn('3D表示を使えません:', err);
            window.coilViewer = null;
            const box = document.getElementById('3d-container');
            const note = document.createElement('p');
            note.className = 'error-message';
            note.style.padding = '16px';
            note.textContent = 'このPCでは3D表示を使えません(WebGL が無効です)。計算とグラフは使えます。';
            box.replaceChildren(note);
            return;
        }

        // 既存の入力値を取得して3Dモデルを更新
        const thickness = parseFloat(document.getElementById('thickness').value);
        const innerDiameter = parseFloat(document.getElementById('inner-diameter').value);
        const coilWidth = parseFloat(document.getElementById('coil-width').value);
        const plateThickness = parseFloat(document.getElementById('plate-thickness').value);

        window.coilViewer.updateCoilModel(thickness, innerDiameter, coilWidth, plateThickness);

        // 入力値変更イベントをリッスン
        const inputFields = ['thickness', 'inner-diameter', 'coil-width', 'plate-thickness'];
        inputFields.forEach(id => {
            document.getElementById(id).addEventListener('input', () => {
                const thickness = parseFloat(document.getElementById('thickness').value);
                const innerDiameter = parseFloat(document.getElementById('inner-diameter').value);
                const coilWidth = parseFloat(document.getElementById('coil-width').value);
                const plateThickness = parseFloat(document.getElementById('plate-thickness').value);

                if (!isNaN(thickness) && !isNaN(innerDiameter) && 
                    !isNaN(coilWidth) && !isNaN(plateThickness)) {
                    window.coilViewer.updateCoilModel(thickness, innerDiameter, coilWidth, plateThickness);
                }
            });
        });

        // コントロールボタンの設定
        document.getElementById('toggle-rotation').addEventListener('click', () => {
            window.coilViewer.setAutoRotate(!window.coilViewer.controls.autoRotate);
        });

        document.getElementById('reset-camera').addEventListener('click', () => {
            window.coilViewer.resetCamera();
        });

        document.getElementById('toggle-details').addEventListener('click', () => {
            const infoDiv = document.querySelector('.coil-viewer-info');
            if (infoDiv) {
                infoDiv.style.display = infoDiv.style.display === 'none' ? 'block' : 'none';
            }
        });

        // カラーピッカー
        document.getElementById('coil-color').addEventListener('input', (e) => {
            window.coilViewer.setCoilColor(e.target.value);
        });

        document.getElementById('bg-color').addEventListener('input', (e) => {
            window.coilViewer.setBackgroundColor(e.target.value);
        });

        // 材質プリセット切替時に3Dビューの色を変更
        document.getElementById('material-preset').addEventListener('change', (e) => {
            const selectedOption = e.target.options[e.target.selectedIndex];
            const materialColor = selectedOption.getAttribute('data-color');
            const specificGravity = selectedOption.getAttribute('data-gravity');

            // 比重を更新
            document.getElementById('specific-gravity').value = specificGravity;

            // 3Dビューの色を更新
            window.coilViewer.setCoilColor(materialColor);

            // カラーピッカーの表示も更新
            document.getElementById('coil-color').value = materialColor;
        });
    }

    // ページロード時に3Dビューアを初期化
    initializeCoilViewer();
});
