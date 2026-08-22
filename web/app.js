function saveToken(){

    const value =
    document.getElementById("tokenInput").value.trim();


    if(!value)
        return;


    localStorage.setItem(
        "token",
        value
    );


    document.getElementById("loginBox").style.display="none";


    tasks();

}



function logout(){

    localStorage.removeItem("token");


    document.getElementById("loginBox").style.display="block";

}



function checkLogin(){

    if(localStorage.getItem("token")){

        document.getElementById("loginBox").style.display="none";

    }

}




function headers(){

    return {

        "Authorization":
        "Bearer " + 
        (localStorage.getItem("token") || "")

    };

}





function clearInput(){

    document.getElementById("links").value="";

}





async function addTracks(){


    const text =
    document.getElementById("links").value;


    const links =
    text
    .split("\n")
    .map(x=>x.trim())
    .filter(Boolean);



    const result =
    document.getElementById("addResult");



    try{


        const r =
        await fetch("/api/add",{

            method:"POST",

            headers:{
                ...headers(),
                "Content-Type":"application/json"
            },


            body:
            JSON.stringify({
                links
            })

        });



        const data =
        await r.json();


        result.innerHTML =
        JSON.stringify(data);


        tasks();


    }catch(e){

        result.innerHTML =
        e.message;

    }


}







async function health(){

    try{


        const r =
        await fetch("/health");


        const data =
        await r.json();



        document.getElementById("health").innerHTML = `

        <div class="status">
        Status: ${data.status}
        </div>

        <div class="status">
        Database: ${data.database}
        </div>

        <div class="status">
        Library: ${data.library}
        </div>

        <div class="status">
        Workers: ${data.workers}
        </div>

        <div class="status">
        Queue: ${data.queue_size}
        </div>

        `;


    }catch(e){

        document.getElementById("health").innerHTML =
        "Offline";

    }

}








async function tasks(){

try{


const r =
await fetch(
"/api/tasks",
{
headers:headers()
}
);



if(!r.ok)
    throw Error();



const data =
await r.json();



document.getElementById("queueCount").innerHTML =
data.length + " tracks";



const box =
document.getElementById("tasks");


box.innerHTML="";



for(const t of data){


const card =
document.createElement("div");


card.className =
"track";



card.innerHTML = `

<div class="cover">
🎵
</div>


<div class="track-info">

<div class="track-title">
${t.title || "Processing..."}
</div>


<div class="track-artist">
${t.artist || ""}
</div>


<small>
${t.url}
</small>


</div>


<div class="track-status ${t.status}">
${t.status}
</div>


`;



box.appendChild(card);


}



}catch(e){

}

}







setInterval(()=>{

health();

tasks();

},3000);




checkLogin();

health();

tasks();
