# Email MCP

Email MCP lets an assistant read your iCloud or Yahoo mail through Home Assistant.
Sending is **off** until you turn it on, and even then it can only write to addresses you list.

You configure everything in Home Assistant. There is no YAML file to edit.

This add-on runs [mcp-email-server](https://github.com/wh1isper/mcp-email-server) by wh1isper (BSD 3-Clause License). The code in this repository is MIT.

## What you are installing

Two pieces work together:

1. **This add-on** talks to your mail provider. It listens only inside Home Assistant, on port 9560, and asks for a bearer token. That port should stay disabled on the Network page so it is not opened on your LAN.
2. **The Email MCP Bridge integration** (in this same repository) gives your assistant a Home Assistant webhook. Home Assistant Cloud does not forward add-on ports, so the integration is what a remote client actually connects to.

```
Assistant  →  Home Assistant webhook  →  Email MCP Bridge  →  this add-on  →  iCloud or Yahoo
```

## Before you start

- Home Assistant OS or Supervised, on a 64-bit PC (`amd64`). Other architectures are not built.
- An iCloud and/or Yahoo mailbox, with an **app password** (not your everyday password).
- The Advanced SSH & Web Terminal add-on, or another way to copy files onto the machine, while this GitHub repository is private.

## Install the add-on while this repository is private

Home Assistant cannot install add-ons from a private GitHub repository. Copy the add-on folder in by hand:

1. On a computer that can see this repository, download it (GitHub → Code → Download ZIP) and unzip it.
2. Copy the `email_mcp` folder to `/addons/email_mcp` on the Home Assistant machine. With SSH, from the unzipped repository:

   ```bash
   scp -r email_mcp root@<your-home-assistant-host>:/addons/email_mcp
   ```

   Or, in the Terminal add-on, create `/addons/email_mcp` and copy the files there. Copy the folder itself, not the whole repository.
3. Copy the integration at the same time (you will need it later):

   ```bash
   scp -r custom_components/email_mcp_bridge root@<your-home-assistant-host>:/config/custom_components/
   ```

   The result on the machine is `/config/custom_components/email_mcp_bridge/`.
4. In Home Assistant go to **Settings → Add-ons → Add-on store**, open the menu (⋮), and choose **Check for updates**.
5. Under **Local add-ons**, open **Email MCP (iCloud / Yahoo)** and choose **Install**. Home Assistant builds it on this machine. The first build downloads the mail-server image (about 63 MB). There is no pre-published image to pull instead.

When this repository is **public**, you can skip the copy. In the add-on store menu choose **Repositories**, add:

`https://github.com/Madseason41/ha-email-mcp`

Then install **Email MCP (iCloud / Yahoo)** from the store. Home Assistant still builds it locally.

## Create an iCloud app password

1. Open [Apple Account](https://account.apple.com) and sign in.
2. Open **Sign-In and Security**.
3. Open **App-Specific Passwords** and choose **+**. Two-factor authentication must already be on.
4. Name it something you will recognize, such as `Home Assistant`.
5. Copy the password Apple shows. You will paste it once into the add-on. It is not your Apple ID password.

Use your full `@icloud.com` or `@me.com` address as the email. If Apple ID sign-in uses another address and IMAP login fails, set **IMAP username** to the mailbox name (the part before `@icloud.com`).

Revoking that app password at Apple immediately disconnects this add-on.

## Create a Yahoo app password

1. Open Yahoo **Account Info** and then **Account Security**.
2. Choose **Generate app password** (Yahoo sometimes labels this **Generate and manage app passwords**).
3. Copy the password Yahoo shows. It is a 16-character app password, not your Yahoo login password.

Use the full `@yahoo.com` address as the email. Revoking the app password at Yahoo immediately disconnects this add-on.

## Fill in the Configuration tab

Open the add-on and choose **Configuration**. Select **Save** when you are done. If the add-on is already running, restart it so the new settings apply.

For iCloud or Yahoo you only fill four things inside **Accounts**. Leave every other account field blank. Blank optional fields use the provider's normal servers:

| Provider | Incoming mail | Outgoing mail |
| --- | --- | --- |
| icloud | `imap.mail.me.com` port 993, SSL | `smtp.mail.me.com` port 587, STARTTLS |
| yahoo | `imap.mail.yahoo.com` port 993, SSL | `smtp.mail.yahoo.com` port 465, SSL |

### Accounts

Choose **Add** and fill in:

- **Account name.** A short id, such as `icloud` or `yahoo`. Letters, digits, `_`, and `-` only. The assistant uses this name when you ask it about a mailbox.
- **Email address.** The full address.
- **Provider.** `icloud`, `yahoo`, or `custom`.
- **App password.** The app password from the steps above.

Leave these blank unless you need them:

- **Display name.** Used on mail you send. Blank uses the part before `@`.
- **IMAP username** and **SMTP username.** Blank means "use the email address".
- **IMAP host / port / security** and **SMTP host / port / security.** Blank uses the preset. For **custom**, IMAP host is required. Leave SMTP host blank for a read-only custom account. Security is `ssl`, `starttls`, or `none`. Use `none` only for a mail server on your own network.
- **Verify mail server certificate.** Leave unset (certificates are checked). Turn it off only for a server you trust that uses a private certificate.
- **Save sent mail to Sent.** Leave unset to keep a copy of mail you send.
- **Drafts mailbox** and **Sent mailbox.** Leave blank unless the automatic folder is wrong. Use the exact folder name.

You can add a second account the same way.

### The other settings

- **Allow sending and drafts.** Leave **off**. Off means read-only: the assistant cannot send, save drafts, move, delete, or mark mail as read. On allows drafts and sending only. Move, delete, and mark-as-read stay blocked either way.
- **Allowed recipients.** Used only when sending is on. Put exact addresses, or a pattern such as `*@example.com`. An empty list blocks every recipient, even if sending is on. Do not enter a single `*` unless you really want mail to go anywhere.
- **Allowed senders.** Leave empty. Empty does not hide mail. Add patterns only if you want reading limited to those From addresses.
- **Bearer token.** Leave empty the first time. Start the add-on and it creates a long random token, stores it privately, and writes it back into this field (shown masked). Use the reveal control to copy it. If you type your own, it must be at least 32 characters. The log never prints the token.
- **Let the assistant read attachment contents.** Leave **off**. On lets a tool return attachment bytes to the assistant. Files are still never saved to disk.
- **Log level.** `info` is enough. `debug` is noisier and still does not log passwords, the token, or webhook addresses.
- **Explain blocked actions.** Leave **on** so the assistant is told why an action was refused.

### Network

Leave port **9560** disabled (shown as blank / not exposed). Home Assistant Core reaches the add-on internally at `http://local-email-mcp:9560`. Turning the port on would publish it on the host.

## Start the add-on and copy the token

1. Start the add-on. The log should mention that the MCP endpoint is up and that bearer auth is required. It should not contain your app password or token.
2. Open **Configuration** again. **Bearer token** now has a value. Reveal it and copy it. You will paste it into the integration and into your assistant.
3. If the field is still empty, the log will say a token was generated but could not be saved back. It is still stored in the add-on's private data. Set a token of your own (at least 32 characters), save, and restart.

## Install the Email MCP Bridge integration

The integration has no YAML setup. Every setting is in its dialogs.

**While this repository is private,** you already copied `custom_components/email_mcp_bridge` in the add-on step. Restart Home Assistant (**Settings → System → Restart**). Then go to **Settings → Devices & services → Add integration**, search for **Email MCP Bridge**, and open it.

**After the repository is public,** you can instead add it in HACS as a custom repository (`https://github.com/Madseason41/ha-email-mcp`, category **Integration**), download it, and restart. The configuration is still the UI dialog, not YAML.

In the dialog:

1. **Add-on URL:** `http://local-email-mcp:9560` (the suggested value).
2. **Bearer token:** the token you copied from the add-on.

The dialog checks the token with a live call. If it fails, the add-on is not running, the URL is wrong, or the token does not match.

Submit. Home Assistant creates a webhook. Only one bridge can be installed.

## Find the webhook address

Open the integration and choose **Configure**. This screen is for admins. It shows three addresses. Treat them like passwords. They are not written to the log.

1. **Remote UI webhook (use this).** Headers and streaming work. It looks like your normal remote Home Assistant address, then `/api/webhook/`, then a long id.
2. **Cloud webhook.** See the limits below. Prefer the remote UI address.
3. **Local path.** The same `/api/webhook/...` path on any address where you reach Home Assistant, including on your LAN.

## Connect an MCP client

In the client that will read your mail:

- Transport: **Streamable HTTP**
- URL: the remote UI webhook address from the integration's Configure screen
- Header: `Authorization: Bearer <the add-on token>`

Ask it to list mail for the account name you chose (`icloud`, `yahoo`, or whatever you typed). With sending left off, asking it to send should fail.

If you rotate the token, update the integration with **Reconfigure** and update the client. Reconfigure keeps the same webhook address.

## Cloud webhook limits

Home Assistant Cloud's cloud webhook is not a normal HTTP connection:

- It does not stream, and it does not return extra headers such as a session id.
- It may also drop the incoming `Authorization` header.

Because of that, the add-on answers every request with a single JSON body and does not require a session. That works through the cloud webhook, but the remote UI address is still the one to use.

If your client reaches the cloud webhook and the token header disappears, open the integration's **Configure** screen and turn on **Cloud webhook: accept requests without Authorization**. Home Assistant then adds the token itself, only when the request arrived with no Authorization header. A wrong token is still rejected. The remote UI address always requires the token. With that option on, anyone who has the cloud webhook address can read your mail, so prefer the remote UI address and leave the option off.

## If you turn sending on

1. In the add-on Configuration tab, turn **Allow sending and drafts** on.
2. Add every address that may receive mail to **Allowed recipients**. Patterns like `*@example.com` are allowed. An empty list still blocks all sending.
3. Save and restart the add-on.
4. Move, delete, and mark-as-read stay blocked. Attachment files are still not written to disk.

Turn sending off again by switching **Allow sending and drafts** off and restarting.

## Remove it

1. **Settings → Devices & services → Email MCP Bridge → Delete.** This removes the webhook and the cloud webhook.
2. **Settings → Add-ons → Email MCP → Stop**, then **Uninstall**.
3. Over SSH, delete the copied folders:

   ```bash
   rm -rf /addons/email_mcp /config/custom_components/email_mcp_bridge
   ```

4. Restart Home Assistant.
5. Revoke the iCloud and Yahoo app passwords you created for it.

## Security

- The integration checks the bearer token, and the add-on checks it again. A request without the token gets "unauthorized" and never reaches your mailbox. Only a small health check is open, and it returns no mail.
- The default is read-only. Sending needs both the switch and an allow-list.
- App passwords, the bearer token, and the webhook addresses are not written to the log.
- Home Assistant stores add-on options, including app passwords, in its own data. Anyone with the webhook address and the token can read the mailboxes you configured. If either leaks, clear the token (and delete the add-on's stored token by reinstalling), set a new one, and revoke the app passwords.
- Mail can contain instructions aimed at the assistant. Leave sending off unless you need it.
