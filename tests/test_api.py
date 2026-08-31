def test_health(client):
    response = client.get("/health")
    assert response.status_code == 200
    assert response.json() == {"status": "ok"}


def test_register_login_and_logout(client):
    payload = {"email": "USER@example.com", "password": "test-pass-123", "display_name": "Test User"}
    registered = client.post("/api/auth/register", json=payload)
    assert registered.status_code == 201
    assert registered.json()["user"]["email"] == "user@example.com"

    duplicate = client.post("/api/auth/register", json=payload)
    assert duplicate.status_code == 409

    logged_in = client.post("/api/auth/login", json={"email": payload["email"], "password": payload["password"]})
    token = logged_in.json()["access_token"]
    headers = {"Authorization": f"Bearer {token}"}
    assert client.get("/api/messages", headers=headers).status_code == 200
    assert client.post("/api/auth/logout", headers=headers).status_code == 200
    assert client.get("/api/messages", headers=headers).status_code == 401


def test_post_edit_and_like(client, alice):
    alice_data, alice_headers = alice
    created = client.post("/api/messages", headers=alice_headers, json={"body": "First post"})
    assert created.status_code == 201
    post_id = created.json()["id"]

    edited = client.patch(f"/api/messages/{post_id}", headers=alice_headers, json={"body": "Updated post"})
    assert edited.json()["body"] == "Updated post"

    bob = client.post(
        "/api/auth/register",
        json={"email": "bob@example.com", "password": "test-pass-123", "display_name": "Bob"},
    ).json()
    bob_headers = {"Authorization": f"Bearer {bob['access_token']}"}
    liked = client.post(f"/api/messages/{post_id}/like", headers=bob_headers)
    assert liked.json()["like_count"] == 1
    assert liked.json()["liked_by_me"] is True
    assert client.post(f"/api/messages/{post_id}/like", headers=bob_headers).json()["like_count"] == 1
    assert client.patch(f"/api/messages/{post_id}", headers=bob_headers, json={"body": "No"}).status_code == 403


def test_find_and_add_friend(client, alice):
    alice_data, alice_headers = alice
    bob = client.post(
        "/api/auth/register",
        json={"email": "bob@example.com", "password": "test-pass-123", "display_name": "Bob"},
    ).json()["user"]
    search = client.get("/api/users?q=Bob", headers=alice_headers)
    assert [user["id"] for user in search.json()] == [bob["id"]]
    assert client.post("/api/friends", headers=alice_headers, json={"user_id": bob["id"]}).status_code == 201
    assert client.post("/api/friends", headers=alice_headers, json={"user_id": bob["id"]}).status_code == 409
    friends = client.get("/api/friends", headers=alice_headers).json()
    assert friends[0]["display_name"] == "Bob"
