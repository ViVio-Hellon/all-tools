/*
  print-sheet.js — コイル重量・平板重量の計算書を印刷する

  「印刷」を押すと、いま出ている計算の結果から計算書(#print-sheet)を組み立てて
  ブラウザの印刷を開く。数値は画面に出ている文字をそのまま写す(画面と紙で違わない)。
  入力に範囲外があるあいだは計算していないので、印刷もしない。

  紙の端から 5mm 以内には何も置かない(プリンタは縁の約 4mm に印刷できない)。
  余白の決めごとは print-sheet.css にまとめてある。
*/
(() => {
    const $ = (id) => document.getElementById(id);

    const SHAPES = {
        coil: {
            section: 'coil-section',
            title: 'コイル重量計算書',
            inputs: () => [
                ['材質', selectedText('material-preset')],
                ['肉厚', value('thickness', 'mm')],
                ['内径', value('inner-diameter', 'mm')],
                ['コイル幅', value('coil-width', 'mm')],
                ['比重', value('specific-gravity', 'g/cm³')],
                ['板厚', value('plate-thickness', 'mm')]
            ],
            results: () => [
                ['重量', text('weight-result')],
                ['長さ', text('length-result')],
                ['巻き数', text('windings-result')]
            ],
            steps: () => [
                ['基本寸法', 'dimensions-detailed'],
                ['体積', 'volume-detailed'],
                ['重量', 'weight-detailed'],
                ['長さ', 'length-detailed'],
                ['巻き数', 'windings-detailed']
            ],
            viewer: () => window.coilViewer
        },
        plate: {
            section: 'plate-section',
            title: '平板重量計算書',
            inputs: () => [
                ['材質', selectedText('plate-material-preset')],
                ['A', value('plate-a', 'mm')],
                ['B', value('plate-b', 'mm')],
                ['板厚', value('plate-t', 'mm')],
                ['比重', value('plate-specific-gravity', 'g/cm³')]
            ],
            results: () => [
                ['重量', text('plate-weight-result')],
                ['面積', text('plate-area-result')],
                ['体積', text('plate-volume-result')]
            ],
            steps: () => [
                ['面積', 'plate-area-detailed'],
                ['体積', 'plate-volume-detailed'],
                ['重量', 'plate-weight-detailed']
            ],
            viewer: () => window.plateViewer
        }
    };

    function text(id) { return ($(id).textContent || '').trim(); }
    function value(id, unit) { return `${$(id).value} ${unit}`; }
    function selectedText(id) {
        const option = $(id).selectedOptions[0];
        return option ? option.textContent.trim() : '';
    }

    function el(tag, className, content) {
        const node = document.createElement(tag);
        if (className) node.className = className;
        if (content !== undefined) node.textContent = content;
        return node;
    }

    /** 入力に範囲外(赤)があれば、その欄の名前を返す。 */
    function invalidField(shape) {
        const bad = $(SHAPES[shape].section).querySelector('input.error');
        if (!bad) return '';
        const label = document.querySelector(`label[for="${bad.id}"]`);
        return label ? label.childNodes[0].textContent.trim() : bad.id;
    }

    /** 3D の図をいまの向きで1枚の絵にする(使えない PC では出さない)。 */
    function snapshot(viewer) {
        try {
            if (!viewer || !viewer.renderer) return '';
            viewer.renderer.render(viewer.scene, viewer.camera);
            return viewer.renderer.domElement.toDataURL('image/png');
        } catch (err) {
            console.warn('3D の図を印刷に入れられません:', err);
            return '';
        }
    }

    function now() {
        const d = new Date();
        const p = (n) => String(n).padStart(2, '0');
        return `${d.getFullYear()}/${p(d.getMonth() + 1)}/${p(d.getDate())} ${p(d.getHours())}:${p(d.getMinutes())}`;
    }

    function build(shape) {
        const def = SHAPES[shape];
        const sheet = $('print-sheet');

        const head = el('header', 'ps-head');
        head.append(el('h1', 'ps-title', def.title),
                    el('span', 'ps-date', `印刷 ${now()}`));

        const inputs = el('table', 'ps-table ps-inputs');
        const caption = el('caption', '', '入力');
        const body = el('tbody');
        for (const [k, v] of def.inputs()) {
            const tr = el('tr');
            tr.append(el('th', '', k), el('td', '', v));
            body.append(tr);
        }
        inputs.append(caption, body);

        const results = el('div', 'ps-results');
        for (const [k, v] of def.results()) {
            const box = el('div', 'ps-result');
            box.append(el('span', 'ps-result__k', k), el('span', 'ps-result__v', v));
            results.append(box);
        }

        const top = el('div', 'ps-top');
        const left = el('div', 'ps-top__left');
        left.append(inputs);
        top.append(left);
        const image = snapshot(def.viewer());
        if (image) {
            const figure = el('figure', 'ps-figure');
            const img = el('img');
            img.src = image;
            img.alt = '3D の図';
            figure.append(img, el('figcaption', '', '図は画面の 3D 表示(寸法の比は目安)'));
            top.append(figure);
        }

        const steps = el('section', 'ps-steps');
        steps.append(el('h2', 'ps-h2', '計算の経過'));
        for (const [title, id] of def.steps()) {
            const block = el('div', 'ps-step');
            block.append(el('h3', 'ps-h3', title));
            const lines = [...$(id).children].map((c) => c.textContent.replace(/\s+/g, ' ').trim())
                .filter(Boolean);
            for (const line of lines) block.append(el('p', 'ps-line', line));
            steps.append(block);
        }

        const foot = el('footer', 'ps-foot', 'VCフィルム長さ計算 ─ コイル・平板(画面と同じ計算式・同じ丸めで出しています)');
        sheet.replaceChildren(head, results, top, steps, foot);
    }

    function print(shape) {
        const bad = invalidField(shape);
        if (bad) {
            window.alert(`「${bad}」が範囲外なので、計算していません。直してから印刷してください。`);
            return;
        }
        build(shape);
        window.print();
    }

    function refreshButtons() {
        for (const button of document.querySelectorAll('.print-btn')) {
            const bad = invalidField(button.dataset.print);
            button.disabled = Boolean(bad);
            button.title = bad ? `「${bad}」が範囲外なので印刷できません` : '計算書を印刷します(A4 縦)';
        }
    }

    document.addEventListener('DOMContentLoaded', () => {
        for (const button of document.querySelectorAll('.print-btn')) {
            button.addEventListener('click', () => print(button.dataset.print));
        }
        document.addEventListener('input', () => setTimeout(refreshButtons, 0));
        document.addEventListener('change', () => setTimeout(refreshButtons, 0));
        refreshButtons();
    });

    // 試験用(印刷ダイアログを開かずに計算書だけ組み立てる)
    window.buildPrintSheet = build;
})();
