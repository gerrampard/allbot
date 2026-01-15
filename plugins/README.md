# WeChat Bridge API 文档

本文档详细介绍了 WeChat Bridge 提供的 API 接口，用于与微信客户端进行交互。

## 服务管理

### 启动微信注入

*   **功能描述**: 启动并注入微信进程，使其能够接收指令和发送消息。这是所有操作的第一步。
*   **URL**: `/api/inject`
*   **HTTP 方法**: `POST`
*   **参数**: 无
*   **返回值**:
    ```json
    {
      "status": "success",
      "client_id": 12345
    }
    ```

### 获取服务状态

*   **功能描述**: 获取当前后端服务和微信客户端的状态信息。
*   **URL**: `/api/status`
*   **HTTP 方法**: `GET`
*   **参数**: 无
*   **返回值**:
    ```json
    {
      "status": "success",
      "data": {
        "service_running": true,
        "client_id": 12345,
        "websocket_connections": 1,
        "wechat_version": "3.9.8.15",
        "dll_version": "4.1.2.17"
      }
    }
    ```

## 消息发送

### 发送文件、图片或视频消息（推荐）

这是推荐使用的统一文件消息发送接口，功能强大且灵活。它支持通过**文件上传**、**文件URL**或**Base64**三种方式发送图片、视频或普通文件。

*   **功能描述**: 智能发送文件、图片或视频。后端会自动处理文件下载、解码，并根据文件类型判断使用何种方式发送。
*   **URL**: `/api/send_file_message`
*   **HTTP 方法**: `POST`
*   **参数**:
    *   使用 `multipart/form-data` 格式提交。
    *   **必须三选一**提供文件来源：`file`, `file_url`, `file_base64`。

| 参数名 | 类型 | 是否必须 | 描述 |
| :--- | :--- | :--- | :--- |
| `to_wxid` | string | 是 | 接收者的微信ID（好友wxid或群聊room_wxid）。 |
| `file` | file | 否 | 通过文件上传方式提供。 |
| `file_url` | string | 否 | 提供一个可公开访问的文件URL。 |
| `file_base64` | string | 否 | 提供文件的Base64编码字符串。 |

*   **使用示例**:

    1.  **通过文件上传**:
        ```bash
        curl -X POST "http://127.0.0.1:8000/api/send_file_message" \
             -F "to_wxid=filehelper" \
             -F "file=@/path/to/your/image.jpg"
        ```

    2.  **通过文件URL**:
        ```bash
        curl -X POST "http://127.0.0.1:8000/api/send_file_message" \
             -F "to_wxid=filehelper" \
             -F "file_url=https://picsum.photos/200"
        ```

    3.  **通过Base64编码**:
        ```bash
        # 首先获取文件的Base64编码
        # Linux/macOS: base64 -i my_video.mp4
        # Windows (PowerShell): [Convert]::ToBase64String([IO.File]::ReadAllBytes("C:\path\to\my_video.mp4"))
        
        curl -X POST "http://127.0.0.1:8000/api/send_file_message" \
             -F "to_wxid=filehelper" \
             -F "file_base64=SUQs..." # 此处为完整的Base64字符串
        ```

*   **返回值**:
    ```json
    {
      "status": "success",
      "message": "File send request dispatched."
    }
    ```

### 发送文本或文件消息（旧版）

这是一个通用的消息发送接口，通过JSON Body接收参数。对于发送文件，它只支持URL或Base64。

*   **功能描述**: 发送文本消息，或通过URL/Base64发送文件/图片/视频。
*   **URL**: `/api/send`
*   **HTTP 方法**: `POST`
*   **请求体 (Request Body)**:
    ```json
    {
      "msg_type": "text", // "text", "image", "video", "file"
      "to_wxid": "filehelper",
      "content": "这是一条测试消息", // msg_type为"text"时必须
      "url": "http://example.com/image.jpg", // msg_type为文件类型时，url或base64二选一
      "base64": "iVBORw0KGgo..." // msg_type为文件类型时，url或base64二选一
    }
    ```
*   **返回值**:
    ```json
    {
      "status": "success",
      "message": "Message send request dispatched."
    }
    ```

## 信息获取

### 获取登录信息

*   **功能描述**: 请求获取当前登录的微信账号信息。这是一个异步请求，结果将通过WebSocket返回。
*   **URL**: `/api/get_login_info`
*   **HTTP 方法**: `GET`
*   **参数**: 无
*   **返回值**:
    ```json
    {
      "status": "success",
      "message": "Request for login info sent. Listen on WebSocket for the response."
    }
    ```
    > **注意**: 最终的用户信息会通过WebSocket以 `MT_USER_LOGIN_EVENT` (10001) 类型的消息推送。

### 获取好友列表

*   **功能描述**: 获取当前账号的所有好友列表。这是一个同步请求，会直接返回结果。
*   **URL**: `/api/get_friend_list`
*   **HTTP 方法**: `GET`
*   **参数**: 无
*   **返回值**:
    ```json
    [
      {
        "wxid": "wxid_xxxx1",
        "nickname": "好友A",
        "remark": "备注A"
      },
      {
        "wxid": "wxid_xxxx2",
        "nickname": "好友B",
        "remark": ""
      }
    ]
    ```

### 获取群聊列表

*   **功能描述**: 获取当前账号加入的所有群聊列表。这是一个同步请求。
*   **URL**: `/api/get_group_list`
*   **HTTP 方法**: `GET`
*   **参数**: 无
*   **返回值**:
    ```json
    [
      {
        "room_wxid": "12345@chatroom",
        "nickname": "群聊A",
        "member_count": 10,
        "owner_wxid": "wxid_xxxx"
      }
    ]
    ```

### 获取群成员列表

*   **功能描述**: 获取指定群聊的所有成员信息。
*   **URL**: `/api/get_group_members/{room_wxid}`
*   **HTTP 方法**: `GET`
*   **路径参数**:
    *   `room_wxid` (string): 目标群聊的ID。
*   **返回值**:
    ```json
    [
      {
        "wxid": "wxid_member1",
        "nickname": "群成员A"
      },
      {
        "wxid": "wxid_member2",
        "nickname": "群成员B"
      }
    ]
    ```

## WebSocket 通信

### 连接WebSocket

*   **功能描述**: 建立WebSocket连接以接收来自微信的实时通知，例如新消息、登录状态变化等。
*   **URL**: `/ws`
*   **协议**: `WebSocket (ws://)`
*   **接收消息格式**:
    所有通过WebSocket推送的消息都遵循以下JSON结构：
    ```json
    {
      "type": 10002, // 消息类型码，例如 10002 代表 MT_RECV_TEXT_MSG
      "data": {
        // 具体的数据结构取决于消息类型
        "from_wxid": "wxid_xxxx",
        "to_wxid": "filehelper",
        "content": "收到的文本消息"
      }
    }

## 消息类型ID参考 (Message Type ID Reference)

下表列出了用于发送各类消息的 `type` ID。部分ID区分个人（私聊）和群聊场景。

| Type ID | Description | Target | Notes / Source |
| :--- | :--- | :--- | :--- |
| `11050` | 发送文本 | 个人 / 群聊 | 通用文本消息ID。 |
| `11051` | 发送群@消息 | 群聊 | 用于在群内@一个或多个成员。 |
| `11040` | 发送图片 | 个人 | 来源于 `routes.py` 中 `MT_SEND_IMGMSG_NORMAL` 的实现。 |
| `11041` | 发送图片 | 群聊 | 根据视频和文件ID规律推断，建议使用此ID发送群图片。 |
| `11053` | 发送图片 (通用) | 个人 / 群聊 | `constants.py` 中定义的通用图片ID，建议优先使用区分场景的ID。 |
| `11042` | 发送视频 | 个人 | 来源于 `routes.py` 中 `MT_SEND_VIDEOMSG_NORMAL` 的实现。 |
| `11043` | 发送视频 | 群聊 | 来源于 `routes.py` 中的实现。 |
| `11054` | 发送视频 (通用) | 个人 / 群聊 | `constants.py` 中定义的通用视频ID。 |
| `11041` | 发送文件 | 个人 / 群聊 | 来源于 `routes.py` 中 `MT_SEND_FILEMSG_NORMAL` 的实现，不区分场景。 |
| `11055` | 发送文件 (通用) | 个人 / 群聊 | `constants.py` 中定义的通用文件ID。 |
| `11058` | 发送GIF动图 | 个人 / 群聊 | - |
| `11056` | 发送名片 | 个人 / 群聊 | - |
| `11057` | 发送链接 | 个人 / 群聊 | - |

## 接收消息类型ID参考 (Received Message Type ID Reference)

| Type ID | Description |
| :--- | :--- |
| `11046` | 文本消息 |
| `11047` | 图片消息 |
| `11048` | 链接消息 |
| `11048` | 语音消息 |
| `11049` | 好友请求消息 |
| `11050` | 名片消息 |
| `11051` | 视频消息 |
| `11052` | 表情消息 |
| `11053` | 位置消息 |
| `11055` | 文件消息 |
| `11056` | 小程序消息 |
| `11057` | 转账消息 |
| `11058` | 系统消息 |
| `11059` | 撤回消息 |
| `11060` | 其他消息 |
| `11061` | 其他应用型消息 |
| `11095` | 二维码收款通知 |