"""画面 URL と API の受付 (Blueprint)。

受け取った入力の**形だけ**を見て、業務ロジック(:mod:`kanban.domain`)と
ビューモデル(:mod:`kanban.presenters`)へ渡す。ここに業務判断は書かない。
"""
