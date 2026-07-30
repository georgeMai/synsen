/*==================================================
    FUTURE TECH PLATFORM
    main.js
==================================================*/

"use strict";

/*==================================================
    DOM
==================================================*/

const loader = document.getElementById("loader");
const navbar = document.querySelector(".navbar");
const menu = document.querySelector(".menu");
const menuBtn = document.querySelector(".menu-btn");

const counters = document.querySelectorAll(".counter");

const revealElements = document.querySelectorAll(
    ".reveal,.fade,.slide-left,.slide-right,.zoom"
);

const heroContent = document.querySelector(".hero-content");
const heroCards = document.querySelector(".hero-card-area");
const hero = document.querySelector(".hero");

const links = document.querySelectorAll(".menu a");

/*==================================================
    LOADER
==================================================*/

window.addEventListener("load", () => {

    setTimeout(() => {

        loader.style.opacity = "0";
        loader.style.visibility = "hidden";

    }, 800);

});

/*==================================================
    NAVBAR SCROLL
==================================================*/

window.addEventListener("scroll", () => {

    if (window.scrollY > 60) {

        navbar.classList.add("scrolled");

    } else {

        navbar.classList.remove("scrolled");

    }

});

/*==================================================
    MOBILE MENU
==================================================*/

menuBtn.addEventListener("click", () => {

    menu.classList.toggle("active");

});

links.forEach(link => {

    link.addEventListener("click", () => {

        menu.classList.remove("active");

    });

});

/*==================================================
    COUNTER
==================================================*/

function startCounter(counter) {

    const target = Number(counter.dataset.target);

    let value = 0;

    const speed = target / 120;

    const timer = setInterval(() => {

        value += speed;

        if (value >= target) {

            value = target;

            clearInterval(timer);

        }

        counter.textContent = Math.floor(value);

    }, 15);

}

const counterObserver = new IntersectionObserver(entries => {

    entries.forEach(entry => {

        if (entry.isIntersecting) {

            startCounter(entry.target);

            counterObserver.unobserve(entry.target);

        }

    });

}, {

    threshold: 0.5

});

counters.forEach(counter => {

    counterObserver.observe(counter);

});

/*==================================================
    SCROLL REVEAL
==================================================*/

function reveal() {

    revealElements.forEach(item => {

        const top = item.getBoundingClientRect().top;

        const windowHeight = window.innerHeight;

        if (top < windowHeight - 120) {

            item.classList.add("show");
            item.classList.add("active");

        }

    });

}

window.addEventListener("scroll", reveal);

window.addEventListener("load", reveal);

/*==================================================
    HERO PARALLAX
==================================================*/

if (hero) {

    hero.addEventListener("mousemove", e => {

        const x = (e.clientX / window.innerWidth - 0.5) * 30;
        const y = (e.clientY / window.innerHeight - 0.5) * 30;

        if (heroContent) {

            heroContent.style.transform =
                `translate(${x}px,${y}px)`;

        }

        if (heroCards) {

            heroCards.style.transform =
                `translate(${-x}px,${-y}px)`;

        }

    });

    hero.addEventListener("mouseleave", () => {

        if (heroContent) {

            heroContent.style.transform = "";

        }

        if (heroCards) {

            heroCards.style.transform = "";

        }

    });

}

/*==================================================
    SMOOTH SCROLL
==================================================*/

document.querySelectorAll("a[href^='#']").forEach(anchor => {

    anchor.addEventListener("click", function (e) {

        const target = document.querySelector(
            this.getAttribute("href")
        );

        if (!target) return;

        e.preventDefault();

        target.scrollIntoView({

            behavior: "smooth"

        });

    });

});

/*==================================================
    ACTIVE MENU
==================================================*/

window.addEventListener("scroll", () => {

    let current = "";

    document.querySelectorAll("section").forEach(section => {

        const top = section.offsetTop - 120;

        if (window.scrollY >= top) {

            current = section.getAttribute("id");

        }

    });

    links.forEach(link => {

        link.classList.remove("active");

        if (link.getAttribute("href") === "#" + current) {

            link.classList.add("active");

        }

    });

});

/*==================================================
    BACK TO TOP BUTTON
==================================================*/

const topBtn = document.createElement("button");

topBtn.innerHTML = "↑";

topBtn.id = "topButton";

document.body.appendChild(topBtn);

Object.assign(topBtn.style, {

    position: "fixed",

    right: "30px",

    bottom: "30px",

    width: "55px",

    height: "55px",

    borderRadius: "50%",

    border: "none",

    background: "#10B981",

    color: "#fff",

    fontSize: "22px",

    cursor: "pointer",

    display: "none",

    zIndex: "999",

    boxShadow: "0 15px 35px rgba(0,0,0,.3)",

    transition: ".3s"

});

window.addEventListener("scroll", () => {

    topBtn.style.display =
        window.scrollY > 300 ? "block" : "none";

});

topBtn.addEventListener("click", () => {

    window.scrollTo({

        top: 0,

        behavior: "smooth"

    });

});

/*==================================================
    DASHBOARD CLOCK
==================================================*/

const dashboardClock = document.getElementById("dashboard-time");

if (dashboardClock) {

    setInterval(() => {

        dashboardClock.textContent =
            new Date().toLocaleTimeString();

    }, 1000);

}

/*==================================================
    RANDOM SENSOR VALUES
==================================================*/

function updateSensor(selector, min, max, unit) {

    const element = document.querySelector(selector);

    if (!element) return;

    const value =
        (Math.random() * (max - min) + min).toFixed(1);

    element.textContent = value + unit;

}

setInterval(() => {

    updateSensor("#temp", 24, 34, "°C");

    updateSensor("#humidity", 50, 95, "%");

    updateSensor("#soil", 20, 90, "%");

    updateSensor("#ph", 5.5, 7.5, "");

}, 3000);

/*==================================================
    FLOATING EFFECT
==================================================*/

document.querySelectorAll(".glass-card").forEach((card, index) => {

    card.style.animationDelay = `${index * 0.5}s`;

});

/*==================================================
    BUTTON RIPPLE
==================================================*/

document.querySelectorAll(".btn-primary").forEach(button => {

    button.addEventListener("click", e => {

        const ripple = document.createElement("span");

        const rect = button.getBoundingClientRect();

        const size = Math.max(rect.width, rect.height);

        ripple.style.width = size + "px";
        ripple.style.height = size + "px";

        ripple.style.position = "absolute";
        ripple.style.borderRadius = "50%";
        ripple.style.background = "rgba(255,255,255,.4)";
        ripple.style.left = (e.clientX - rect.left - size / 2) + "px";
        ripple.style.top = (e.clientY - rect.top - size / 2) + "px";
        ripple.style.transform = "scale(0)";
        ripple.style.transition = "0.6s";
        ripple.style.pointerEvents = "none";

        button.appendChild(ripple);

        requestAnimationFrame(() => {

            ripple.style.transform = "scale(2)";
            ripple.style.opacity = "0";

        });

        setTimeout(() => ripple.remove(), 600);

    });

});

/*==================================================
    TYPEWRITER
==================================================*/

const subtitle = document.querySelector(".subtitle");

if (subtitle) {

    const text = subtitle.textContent;

    subtitle.textContent = "";

    let i = 0;

    function type() {

        if (i < text.length) {

            subtitle.textContent += text.charAt(i);

            i++;

            setTimeout(type, 45);

        }

    }

    setTimeout(type, 1000);

}

/*==================================================
    CONSOLE
==================================================*/

console.log(
`==========================================
 Future Tech Platform
 Robot + IoT + AI + Big Data
 Version 1.0
==========================================`
);

const chatToggle=document.getElementById("chatToggle");
const chatWindow=document.getElementById("chatWindow");
const closeChat=document.getElementById("closeChat");
const sendBtn=document.getElementById("sendBtn");
const chatInput=document.getElementById("chatInput");
const chatBody=document.getElementById("chatBody");

chatToggle.onclick=()=>{

chatWindow.style.display="flex";

}

closeChat.onclick=()=>{

chatWindow.style.display="none";

}

function addMessage(text,type){

const div=document.createElement("div");

div.className="message "+type;

div.innerHTML=text;

chatBody.appendChild(div);

chatBody.scrollTop=chatBody.scrollHeight;

}

function fakeAI(msg){

let reply="";

msg=msg.toLowerCase();

if(msg.includes("iot")){

reply="🌱 Tôi có thể giúp bạn thiết kế hệ thống IoT từ ESP32 → MQTT → Supabase → Dashboard.";

}
else if(msg.includes("robot")){

reply="🤖 Chúng ta có thể xây Robot dùng ESP32, STM32, Raspberry Pi hoặc Jetson Nano.";

}
else if(msg.includes("data")){

reply="📊 Data Mentor cung cấp SQL, Python, Spark, Kafka, Airflow, Lakehouse và Databricks.";

}
else if(msg.includes("nông")){

reply="🌾 Tôi đề xuất giải pháp cảm biến pH, EC, NPK, độ ẩm đất kết hợp AI dự báo tưới.";

}
else{

reply="😊 Cảm ơn câu hỏi của bạn. Phiên bản demo này sẽ được kết nối OpenAI API hoặc mô hình AI của bạn để trả lời thông minh hơn.";

}

setTimeout(()=>{

addMessage(reply,"ai");

},700);

}

sendBtn.onclick=()=>{

const msg=chatInput.value.trim();

if(msg==="") return;

addMessage(msg,"user");

chatInput.value="";

fakeAI(msg);

}

chatInput.addEventListener("keypress",e=>{

if(e.key==="Enter"){

sendBtn.click();

}

});