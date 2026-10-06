"""VC長さ計算 ── vc-calculator(VCフィルム長さ計算 Python Web版)の移植

紙管に巻かれた VC フィルムの**肉厚から残りの長さ(m)**を出し、定尺ごとに
何枚の製品に貼れるかを出します(VBA `VCビニ－ル自動計算 .xlsm` の
UserForm `UFVC計算` / `UFquick`)。VBA が何をしていたかは
`docs/vc/VBA解析.md` にあります。

    vba_compat   VBA の `Format` の丸め(0.5 は 0 から遠い側・有効15桁)
    calc         計算ボタン(VC長さ → 肉厚の逆算 → 枚数)と計算の経過
    seed         VBA の直書きと早見表(R.1.10/1)の初期値
    schema.sql   マスタの表(VC品種・VC内径選択肢・枚数定尺・早見表…)
    db           マスタの作成・版の更新・書く取引・変更履歴・更新番号
    masters      いまの中身(読めなければ控え → 初期値)
    quick_table  早見表(計算画面と同じ式から出す)
    grid         早見表のマスをまとめて作る(管理者)

**計算・丸め・判定の順序は vc-calculator から1つも変えていません**
(`tests/test_vc_calc.py` / `test_vc_master.py` が同じ期待値で確かめます)。
変えたのは載せ方だけです ── 画面は日報管理ツールのレール「VC長さ計算」、
マスタは参照用マスタのフォルダ、直すのは設定・管理者の「マスタ管理」。
"""
