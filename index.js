$(window).scroll(function(){
    if($(document).scrollTop() > 200)
    {
        $('.navbar').addClass('changes')
        $('.imageig').addClass('imageig')
    }
    else
    {       
        $('.navbar').removeClass('changes')
        $('.imageig').removeClass('imageig')
    }   
})


