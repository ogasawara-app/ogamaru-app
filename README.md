# おがまる便り（おがさわら丸・ははじま丸 スケジュールアプリ）

## 含まれるファイル
- `index.html` : 本番用Webアプリ画面
- `ogamaru_updater.py` : 小笠原海運・東京宝島うみそら便から最新情報を取得して `schedule_data.json` を更新・通知するスクリプト
- `.github/workflows/update.yml` : 毎日朝7:00および日中（9:00〜17:50）10分おきに自動実行するGitHub Actions設定

## セットアップ手順
1. GitHubで新しいリポジトリを作成し、このフォルダ内のファイル（`.github` フォルダを含む）をそのままアップロードします。
2. 通知を送りたい場合は、リポジトリの `Settings` → `Secrets and variables` → `Actions` から `LINE_CHANNEL_ACCESS_TOKEN` または `WEBHOOK_URL` を登録します。
3. `Settings` → `Pages` で Branch を `main`（`/ (root)`）に設定して保存すると、公開URLが発行されます。
