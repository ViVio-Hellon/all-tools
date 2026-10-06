"""共通基盤(起動・設定・ログ・自動終了・待機画面・プロセスの記録)。

python-web-tools(梱包資材総合ツール)と同じ基盤を、この道具用に
取り出したもの。**点検表の業務はここに書かない**(業務は `app/`)。

    app_config      アプリという「入れ物」の値(config/app.json・ローカル領域)
    logging_utils   日ごとのログファイル
    idle_exit       画面が居なくなったら終わる(裏に回った画面・スリープを考慮)
    boot_screen     起動待機画面(loading.html を標準ライブラリだけで出す)
    process_tracking このアプリが起動した子プロセス(Excel)を記録して片付ける
"""
