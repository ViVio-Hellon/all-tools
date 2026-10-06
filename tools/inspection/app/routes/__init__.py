"""URLの受付 (Blueprint)

入力を検証して services へ渡すだけの層。業務判断はここに書かない
(python-web-tools と同じ)。各モジュールが `bp` を1つ持ち、
`app._register_routes` が登録する。
"""
