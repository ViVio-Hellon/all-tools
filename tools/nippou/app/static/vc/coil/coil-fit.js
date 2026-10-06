/*
  coil-fit.js — 画面に収める(どの段にするかを決めるだけ。見た目は coil-fit.css)

  いまの中身が要る高さを、高さの縛りを外して(fit-measure)はかり、見えている高さと
  比べて段を決める:
    そのまま → 詰めた見た目(fit-compact) → 0.75 倍まで縮める(fit-zoom)
    → それでも収まらなければページごと縦に動かす(fit-scroll)
  はかるのは同じ処理の中で付けて外すので、途中の姿は画面に出ない。

  はかり直すのは: 窓の大きさが変わったとき / 形状・タブを切り替えたとき /
  中身の高さが変わったとき(グラフが大きさを決めた・エラーの文が出た)/ 読み込みが
  終わったとき。決める段が前と同じなら何もしない(見張りが回り続けない)。
*/
(() => {
    const WIDE = 1401;        // これより狭いと3列に並ばない(元どおりページごと動かす)
    const MIN_ZOOM = 0.75;    // これより小さくすると字が読みにくい
    const root = document.documentElement;
    let last = '';
    let pending = 0;

    const height = () => document.body.getBoundingClientRect().height;

    // 見せている形状の**タブを全部**はかって、いちばん高いものに合わせる。タブを
    // 切り替えるたびに段が変わって見出しが動く(ガタつく)のを避ける
    function measure(compact) {
        root.classList.toggle('fit-compact', compact);
        root.classList.add('fit-measure');
        const section = [...document.querySelectorAll('.shape-section')].find((s) => !s.hidden);
        const panes = section ? [...section.querySelectorAll('.tab-content, .ptab-content')] : [];
        const shown = panes.filter((p) => p.classList.contains('active'));
        let need = panes.length ? 0 : height();
        for (const pane of panes) {
            panes.forEach((p) => p.classList.toggle('active', p === pane));
            need = Math.max(need, height());
        }
        panes.forEach((p) => p.classList.toggle('active', shown.includes(p)));
        root.classList.remove('fit-measure');
        return Math.ceil(need);
    }

    function decide() {
        root.classList.remove('fit-zoom', 'fit-scroll');
        root.style.removeProperty('--fit-zoom');
        if (window.innerWidth < WIDE) {
            root.classList.remove('fit-compact');
            return 'narrow';
        }
        const room = window.innerHeight;
        if (measure(false) <= room) {
            root.classList.remove('fit-compact');
            return 'as-is';
        }
        const need = measure(true);         // 詰めた見た目のまま残る
        if (need <= room) return 'compact';
        const zoom = Math.floor((room / need) * 100) / 100;
        if (zoom >= MIN_ZOOM) {
            root.style.setProperty('--fit-zoom', String(zoom));
            root.classList.add('fit-zoom');
            return `zoom ${zoom}`;
        }
        root.classList.add('fit-scroll');
        return 'scroll';
    }

    function fit() {
        pending = 0;
        const mode = decide();
        root.dataset.fit = mode;
        if (mode === last) return;
        last = mode;
        // 3D は自分では枠の大きさの変化に気づかない(窓の resize だけを見ている)
        for (const viewer of [window.coilViewer, window.plateViewer]) {
            if (viewer && typeof viewer.onWindowResize === 'function') viewer.onWindowResize();
        }
    }

    function later() {
        if (!pending) pending = requestAnimationFrame(fit);
    }

    window.coilFit = { fit, later };

    document.addEventListener('DOMContentLoaded', () => {
        fit();
        window.addEventListener('resize', later);
        window.addEventListener('load', later);
        document.addEventListener('shape-shown', later);
        // タブ・形状の切替は、それぞれの処理が済んでから(ここまで泡が上がってくる)はかる
        document.addEventListener('click', (event) => {
            if (event.target.closest('.tab, .ptab, .shape-btn')) later();
        });
        // 中身の高さが後から変わる: アイコンの字が届くと切替やタブの帯が縮む / グラフは
        // 見せた次の描画で大きさが決まる / エラーの文が出ると入力の欄が伸びる。
        // 中身そのもの(引き伸ばされない箱)を見張る
        if (typeof ResizeObserver === 'function') {
            const watch = new ResizeObserver(later);
            document.querySelectorAll(
                '.shape-switch, header, .tabs, .results, .input-panel > *, .detail-card'
            ).forEach((el) => watch.observe(el));
        }
        document.addEventListener('input', later);
        // 字(アイコンの字も)が読み込まれると高さが変わる。隠れているタブの中は見張りが
        // 気づかないので、字の読み込みが済むたびにはかり直す
        if (document.fonts) {
            if (document.fonts.ready) document.fonts.ready.then(later);
            if (document.fonts.addEventListener) document.fonts.addEventListener('loadingdone', later);
        }
    });
})();
